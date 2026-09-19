"""历史基线：把启用迁移框架之前由 `_lightweight_migrations` 做的补齐动作固化。

这条迁移等价于旧版本 `app/database.py::_lightweight_migrations` 的全部行为，
所以在既有部署上是「把没加过的列补上」，在全新库上是空跑（表还不存在，
随后由 `create_all` 直接建成最新结构）。全部操作幂等。
"""
from __future__ import annotations

DESCRIPTION = "历史基线：补齐既有轻量迁移引入的列与索引"
DESTRUCTIVE = False


def apply(ctx) -> None:
    ctx.add_column("users", "storage_limit_mb", "storage_limit_mb INTEGER")
    ctx.add_column("users", "reg_fp", "reg_fp VARCHAR(128) DEFAULT '' NOT NULL")
    ctx.add_column("users", "reg_ip", "reg_ip VARCHAR(64) DEFAULT '' NOT NULL")
    ctx.add_column("users", "agreement_version", "agreement_version INTEGER DEFAULT 0 NOT NULL")
    ctx.add_column("users", "notice_version", "notice_version INTEGER DEFAULT 0 NOT NULL")
    ctx.add_column("users", "reg_note", "reg_note VARCHAR(300) DEFAULT '' NOT NULL")
    ctx.add_column("users", "last_checkin_key", "last_checkin_key VARCHAR(10) DEFAULT '' NOT NULL")
    ctx.add_column("users", "max_inflight", "max_inflight INTEGER")

    # 仅在本次真正新建 note 列时回填一次；之后不再动，
    # 避免管理员手动清空备注后重启又被还原（沿用旧实现语义）。
    if ctx.add_column("users", "note", "note VARCHAR(500) DEFAULT '' NOT NULL"):
        ctx.execute("UPDATE users SET note = reg_note WHERE reg_note != ''")

    ctx.create_index("users", "ix_users_reg_fp", ["reg_fp"])

    # temp_expires_at 用 TIMESTAMP：PG 没有 DATETIME 类型
    ctx.add_column("user_quotas", "temp_amount", "temp_amount INTEGER DEFAULT 0 NOT NULL")
    ctx.add_column("user_quotas", "temp_expires_at", "temp_expires_at TIMESTAMP")

    ctx.add_column("invite_codes", "pool_type", "pool_type VARCHAR(16) DEFAULT 'permanent' NOT NULL")
    ctx.add_column("invite_codes", "valid_days", "valid_days INTEGER DEFAULT 1 NOT NULL")
    ctx.add_column("invite_codes", "valid_hours", "valid_hours INTEGER DEFAULT 0 NOT NULL")
    ctx.add_column("invite_codes", "fixed_expires_at", "fixed_expires_at TIMESTAMP")

    ctx.add_column("redemption_codes", "fixed_expires_at", "fixed_expires_at TIMESTAMP")
