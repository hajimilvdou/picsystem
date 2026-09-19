"""请求/响应模型（pydantic）。"""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9\-]{4,64}$")


def _check_username(v: str) -> str:
    if not USERNAME_RE.fullmatch(v):
        raise ValueError("用户名需为 3-32 位字母、数字、下划线或连字符")
    return v


def _check_password(v: str) -> str:
    if len(v) < 8 or len(v) > 128:
        raise ValueError("密码长度需为 8-128 位")
    return v


class RegisterIn(BaseModel):
    # 两者同时为空 = 随机生成账号（凭证仅返回一次）
    username: str | None = None
    password: str | None = None
    invite_code: str = Field(min_length=4, max_length=64)
    fp: str = Field(default="", max_length=128)  # 设备指纹
    note: str = Field(default="", max_length=300)  # 审批制注册时的申请备注

    @field_validator("username")
    @classmethod
    def _u(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not USERNAME_RE.fullmatch(v):
            raise ValueError("用户名需为 3-32 位字母、数字、下划线或连字符")
        return v

    @field_validator("password")
    @classmethod
    def _p(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v) < 8 or len(v) > 128:
            raise ValueError("密码长度需为 8-128 位")
        return v

    @model_validator(mode="after")
    def _pair(self):
        if bool(self.username) != bool(self.password):
            raise ValueError("用户名与密码需同时填写，或同时留空以随机生成")
        return self


class RedeemIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str

    _p = field_validator("new_password")(_check_password)


class QuotaInfo(BaseModel):
    total: int  # 永久组；-1 = 不限
    used: int
    temp: int = 0            # 限时组剩余（已过滤过期）
    temp_expires_at: str | None = None


class StorageInfo(BaseModel):
    used_bytes: int
    limit_mb: int  # 0 = 不限


class MeOut(BaseModel):
    id: int
    username: str
    role: str
    status: str
    quotas: dict[str, QuotaInfo]
    storage: StorageInfo
    features: dict[str, bool]
    registration_enabled: bool
    site_name: str
    announcement: str
    agreement_version: int
    agreement_required: int
    checkin_enabled: bool
    checked_in_today: bool


# ---- 对话 ----

class ChatSendIn(BaseModel):
    conversation_id: int | None = None
    model: str = Field(default="auto", max_length=100)
    message: str = Field(min_length=1, max_length=20000)


class ConversationCreateIn(BaseModel):
    title: str = Field(default="新对话", max_length=200)


class ConversationRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# ---- 搜索 ----

class SearchIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


# ---- 绘图 ----

class ImageGenIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    model: str = Field(default="gpt-image-2", max_length=100)
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = Field(default=None, max_length=32)
    quality: str = Field(default="auto", max_length=16)


# ---- PPT ----

class PptGenIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    kind: str = Field(default="ppt", pattern="^(ppt|psd)$")
    base64_images: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("base64_images")
    @classmethod
    def _cap_images(cls, v: list[str]) -> list[str]:
        for item in v:
            if len(item) > 14 * 1024 * 1024:  # base64 编码后约 10MB 原始字节
                raise ValueError("单张参考图不能超过 10MB")
        return v


# ---- API 密钥 ----

class ApiKeyCreateIn(BaseModel):
    name: str = Field(default="", max_length=100)


# ---- 管理端 ----

class AdminUserCreateIn(BaseModel):
    username: str
    password: str
    role: str = Field(default="user", pattern="^(admin|user)$")
    quotas: dict[str, int] = Field(default_factory=dict)  # feature -> total(-1 不限)

    _u = field_validator("username")(_check_username)
    _p = field_validator("password")(_check_password)


class AdminQuotaPatch(BaseModel):
    total: int | None = Field(default=None, ge=-1)  # -1 不限
    reset_used: bool = False


class AdminUserPatchIn(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    quotas: dict[str, AdminQuotaPatch] | None = None
    storage_limit_mb: int | None = Field(default=None, ge=0)  # 0 = 不限
    storage_limit_clear: bool = False  # true = 清除个人覆盖，跟随全局默认
    max_inflight: int | None = Field(default=None, ge=1, le=100)  # 并发上限
    max_inflight_clear: bool = False  # true = 清除个人覆盖，跟随全局
    note: str | None = Field(default=None, max_length=500)  # 管理员备注


class AdminBulkQuotaIn(BaseModel):
    user_ids: list[int] | str = Field(default="all")  # 列表或 "all"
    feature: str = Field(pattern="^(chat|image|search|ppt)$")
    pool: str = Field(default="permanent", pattern="^(permanent|temporary)$")
    mode: str = Field(pattern="^(add|subtract|set)$")  # 加 / 减 / 统一设为
    amount: int = Field(ge=0, le=1000000)
    valid_days: int = Field(default=1, ge=1, le=3650)  # temporary 池有效期
    valid_hours: int = Field(default=0, ge=0, le=8784)
    fixed_expires_at: datetime | None = None  # 固定到期时间（优先于天数+小时）


class AdminResetPasswordIn(BaseModel):
    password: str | None = None  # 为空则自动生成


class InviteCreateIn(BaseModel):
    code: str | None = Field(default=None, max_length=64)
    note: str = Field(default="", max_length=200)
    max_uses: int = Field(default=1, ge=1, le=100000)
    chat_quota: int = Field(default=0, ge=-1)
    image_quota: int = Field(default=0, ge=-1)
    search_quota: int = Field(default=0, ge=-1)
    ppt_quota: int = Field(default=0, ge=-1)
    pool_type: str = Field(default="permanent", pattern="^(permanent|temporary)$")
    valid_days: int = Field(default=1, ge=1, le=3650)
    valid_hours: int = Field(default=0, ge=0, le=8784)
    fixed_expires_at: datetime | None = None  # 固定到期时间（优先于天数+小时）
    expires_at: datetime | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        if v is not None and not INVITE_CODE_RE.fullmatch(v):
            raise ValueError("码需为 4-64 位字母、数字或连字符")
        return v


class InvitePatchIn(BaseModel):
    note: str | None = Field(default=None, max_length=200)
    max_uses: int | None = Field(default=None, ge=1, le=100000)
    enabled: bool | None = None
    chat_quota: int | None = Field(default=None, ge=-1)
    image_quota: int | None = Field(default=None, ge=-1)
    search_quota: int | None = Field(default=None, ge=-1)
    ppt_quota: int | None = Field(default=None, ge=-1)
    pool_type: str | None = Field(default=None, pattern="^(permanent|temporary)$")
    valid_days: int | None = Field(default=None, ge=1, le=3650)
    valid_hours: int | None = Field(default=None, ge=0, le=8784)
    fixed_expires_at: datetime | None = None
    expires_at: datetime | None = None


class SettingsPatchIn(BaseModel):
    feature_chat_enabled: bool | None = None
    feature_image_enabled: bool | None = None
    feature_search_enabled: bool | None = None
    feature_ppt_enabled: bool | None = None
    registration_enabled: bool | None = None
    site_name: str | None = Field(default=None, max_length=100)
    announcement: str | None = Field(default=None, max_length=2000)
    upstream_base_url: str | None = Field(default=None, max_length=300)
    upstream_api_key: str | None = Field(default=None, max_length=300)
    user_rate_limit_per_minute: int | None = Field(default=None, ge=1, le=1000)
    login_max_failures: int | None = Field(default=None, ge=3, le=50)
    login_lock_minutes: int | None = Field(default=None, ge=1, le=1440)
    register_max_per_ip_day: int | None = Field(default=None, ge=1, le=1000)
    storage_quota_mb_default: int | None = Field(default=None, ge=0, le=1048576)
    file_retention_hours: int | None = Field(default=None, ge=0, le=87600)
    log_retention_hours: int | None = Field(default=None, ge=0, le=87600)
    audit_retention_days: int | None = Field(default=None, ge=0, le=3650)
    register_max_per_fp_total: int | None = Field(default=None, ge=1, le=100)
    registration_require_approval: bool | None = None
    agreement_version: int | None = Field(default=None, ge=1, le=1000000)
    agreement_text: str | None = Field(default=None, max_length=20000)
    notice_version: int | None = Field(default=None, ge=1, le=1000000)
    notice_text: str | None = Field(default=None, max_length=5000)
    content_filter_enabled: bool | None = None
    content_filter_keywords: str | None = Field(default=None, max_length=20000)
    checkin_enabled: bool | None = None
    checkin_pool: str | None = Field(default=None, pattern="^(permanent|temporary)$")
    checkin_valid_days: int | None = Field(default=None, ge=1, le=3650)
    checkin_valid_hours: int | None = Field(default=None, ge=0, le=8784)
    checkin_timezone: str | None = Field(default=None, max_length=64)
    checkin_chat: int | None = Field(default=None, ge=0, le=100000)
    checkin_image: int | None = Field(default=None, ge=0, le=100000)
    checkin_search: int | None = Field(default=None, ge=0, le=100000)
    checkin_ppt: int | None = Field(default=None, ge=0, le=100000)
    max_inflight_default: int | None = Field(default=None, ge=1, le=100)


class UpstreamTestIn(BaseModel):
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=300)
