"""数据模型。额度设计为按次数（quota_total=-1 表示不限）；
usage_logs 预留 prompt_tokens / completion_tokens / cost 字段，
便于将来扩展 token 计费与价格体系。"""
from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Feature(str, enum.Enum):
    CHAT = "chat"
    IMAGE = "image"
    SEARCH = "search"
    PPT = "ppt"


FEATURES: tuple[str, ...] = (f.value for f in Feature)

FEATURE_LABELS = {
    Feature.CHAT.value: "对话",
    Feature.IMAGE.value: "绘图",
    Feature.SEARCH.value: "搜索",
    Feature.PPT.value: "PPT/PSD",
}


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    PENDING = "pending"  # 审批制注册：待管理员审核


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(sa.String(256))
    role: Mapped[str] = mapped_column(sa.String(16), default=UserRole.USER.value)
    status: Mapped[str] = mapped_column(sa.String(16), default=UserStatus.ACTIVE.value)
    invite_code_id: Mapped[int | None] = mapped_column(sa.ForeignKey("invite_codes.id"), nullable=True)
    token_version: Mapped[int] = mapped_column(sa.Integer, default=0)  # 改密/重置后使旧会话失效
    # 个人存储配额（MB）；NULL 跟随全局默认 storage_quota_mb_default，0 = 不限
    storage_limit_mb: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # 注册时设备指纹（防单人多注册风控），仅加盐哈希存储
    reg_fp: Mapped[str] = mapped_column(sa.String(128), default="", index=True)
    # 注册来源 IP（注册防刷按 users 表统计，不依赖日志保留期）
    reg_ip: Mapped[str] = mapped_column(sa.String(64), default="")
    # 已同意的协议版本；低于全局 agreement_version 时需重新阅读并同意
    agreement_version: Mapped[int] = mapped_column(sa.Integer, default=0)
    # 已阅读的公告版本；低于全局 notice_version 时登录后弹一次公告
    notice_version: Mapped[int] = mapped_column(sa.Integer, default=0)
    # 审批制注册时的申请备注（管理员可见）
    reg_note: Mapped[str] = mapped_column(sa.String(300), default="")
    # 管理员备注（注册时自动带入申请备注，可在用户管理中修改）
    note: Mapped[str] = mapped_column(sa.String(500), default="")
    # 当日签到键（按 checkin_timezone 的日期，如 2026-09-19）
    last_checkin_key: Mapped[str] = mapped_column(sa.String(10), default="")
    # 单用户并发上限覆盖；NULL 跟随全局 max_inflight_default
    max_inflight: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    quotas: Mapped[list["UserQuota"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    note: Mapped[str] = mapped_column(sa.String(200), default="")
    max_uses: Mapped[int] = mapped_column(sa.Integer, default=1)
    used_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    chat_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    image_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    search_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    ppt_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    # 额度池：permanent=永久组；temporary=限时组（按 valid_days/valid_hours 计算到期）
    pool_type: Mapped[str] = mapped_column(sa.String(16), default="permanent")
    valid_days: Mapped[int] = mapped_column(sa.Integer, default=1)    # 限时：N 个自然日（1=当天 24 点）
    valid_hours: Mapped[int] = mapped_column(sa.Integer, default=0)   # 限时：附加小时
    # 固定到期时间（优先于 天数+小时 的相对规则；设置后无论何时发放都在此点失效）
    fixed_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class UserQuota(Base):
    __tablename__ = "user_quotas"

    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    feature: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    quota_total: Mapped[int] = mapped_column(sa.Integer, default=0)  # 永久组：-1 = 不限
    quota_used: Mapped[int] = mapped_column(sa.Integer, default=0)   # 永久组已用
    # 限时组：到期前可优先消耗，过期自动作废
    temp_amount: Mapped[int] = mapped_column(sa.Integer, default=0)
    temp_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="quotas")


class RedemptionCode(Base):
    """兑换码：纯额度兑换（老用户使用），与注册邀请码相互独立。"""

    __tablename__ = "redemption_codes"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    note: Mapped[str] = mapped_column(sa.String(200), default="")
    max_uses: Mapped[int] = mapped_column(sa.Integer, default=1)
    used_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    chat_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    image_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    search_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    ppt_quota: Mapped[int] = mapped_column(sa.Integer, default=0)
    pool_type: Mapped[str] = mapped_column(sa.String(16), default="permanent")
    valid_days: Mapped[int] = mapped_column(sa.Integer, default=1)
    valid_hours: Mapped[int] = mapped_column(sa.Integer, default=0)
    fixed_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class Redemption(Base):
    """兑换记录：每用户对同一兑换码只能成功兑换一次。"""

    __tablename__ = "redemptions"
    __table_args__ = (sa.UniqueConstraint("user_id", "redemption_code_id"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    redemption_code_id: Mapped[int] = mapped_column(sa.ForeignKey("redemption_codes.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(sa.String(200), default="新对话")
    model: Mapped[str] = mapped_column(sa.String(100), default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(sa.ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(sa.String(16))  # user / assistant / system
    content: Mapped[str] = mapped_column(sa.Text, default="")
    meta: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)  # 引用来源等
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(100), default="")
    prefix: Mapped[str] = mapped_column(sa.String(20))  # 展示用前缀，如 sk-ab12cd
    key_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)  # sha256
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    api_key_id: Mapped[int | None] = mapped_column(sa.ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    feature: Mapped[str] = mapped_column(sa.String(16), index=True)
    model: Mapped[str] = mapped_column(sa.String(100), default="")
    endpoint: Mapped[str] = mapped_column(sa.String(64), default="")
    status: Mapped[str] = mapped_column(sa.String(16), default="success")  # success / failed
    error: Mapped[str] = mapped_column(sa.String(500), default="")
    latency_ms: Mapped[int] = mapped_column(sa.Integer, default=0)
    units: Mapped[int] = mapped_column(sa.Integer, default=1)  # 消耗次数（绘图按张数）
    prompt_tokens: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)  # 预留：token 计费
    completion_tokens: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(sa.Float, nullable=True)  # 预留：价格体系
    ip: Mapped[str] = mapped_column(sa.String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
    )


class StoredFile(Base):
    __tablename__ = "stored_files"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(sa.String(16), default="image")  # image / ppt / psd
    filename: Mapped[str] = mapped_column(sa.String(256))
    path: Mapped[str] = mapped_column(sa.String(512))  # 相对 DATA_DIR 的路径
    mime: Mapped[str] = mapped_column(sa.String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(sa.Integer, default=0)
    prompt: Mapped[str] = mapped_column(sa.Text, default="")
    # 图库标签：逗号分隔存放（标签本身不允许逗号，见 services/tags.py）
    tags: Mapped[str] = mapped_column(sa.String(300), default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class PptTask(Base):
    """PPT/PSD 任务：跟踪上游任务状态，完成后落到本地文件。"""

    __tablename__ = "ppt_tasks"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)  # 本站任务 id
    upstream_task_id: Mapped[str] = mapped_column(sa.String(200), default="")
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    api_key_id: Mapped[int | None] = mapped_column(sa.ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(sa.String(8), default="ppt")  # ppt / psd
    prompt: Mapped[str] = mapped_column(sa.Text, default="")
    status: Mapped[str] = mapped_column(sa.String(16), default="queued")  # queued/running/success/error
    error: Mapped[str] = mapped_column(sa.String(500), default="")
    file_id: Mapped[int | None] = mapped_column(sa.ForeignKey("stored_files.id", ondelete="SET NULL"), nullable=True)
    refunded: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(sa.JSON, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(sa.String(64))
    target: Mapped[str] = mapped_column(sa.String(128), default="")
    detail: Mapped[str] = mapped_column(sa.String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class RiskEvent(Base):
    """风控事件：登录失败、触发锁定、限流、注册拦截等。"""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(sa.String(32), index=True)  # login_fail / login_locked / rate_limit / register / register_blocked
    username: Mapped[str] = mapped_column(sa.String(64), default="", index=True)
    user_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    ip: Mapped[str] = mapped_column(sa.String(64), default="", index=True)
    detail: Mapped[str] = mapped_column(sa.String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
    )


class UserSession(Base):
    """登录设备会话：每次登录创建一条，可单独吊销（踢出设备）。"""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)  # uuid
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ip: Mapped[str] = mapped_column(sa.String(64), default="")
    user_agent: Mapped[str] = mapped_column(sa.String(300), default="")
    device_label: Mapped[str] = mapped_column(sa.String(120), default="")
    revoked: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
