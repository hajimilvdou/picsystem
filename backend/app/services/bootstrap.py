"""首次启动初始化：默认设置与初始管理员。"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import User, UserQuota, UserRole
from ..security import hash_password
from .settings_store import DEFAULT_SETTINGS

logger = logging.getLogger("picsystem.bootstrap")


async def seed_defaults(session: AsyncSession) -> None:
    # 只写入尚不存在的设置键，绝不覆盖管理员在后台已修改的值
    from ..models import Setting

    existing = {row.key for row in (await session.execute(select(Setting.key))).all()}
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(Setting(key=key, value=value))
    await session.flush()

    # 一次性默认值迁移：早期版本的 seed 每次启动都会把设置重置为旧默认
    # （storage_quota_mb_default=0、file_retention_hours=0），因此老库中的
    # 这些值事实上就是默认值而非管理员刻意配置，升级时跟随新默认。
    # 迁移只执行一次（由标记键记录），之后管理员显式设 0 不会被改写。
    MIGRATION_FLAG = "_defaults_migrated_v2"
    migrated = await session.get(Setting, MIGRATION_FLAG)
    if migrated is None:
        LEGACY_DEFAULT_MIGRATIONS = {
            "storage_quota_mb_default": (0, 100),
            "file_retention_hours": (0, 24),
        }
        for key, (old_default, new_default) in LEGACY_DEFAULT_MIGRATIONS.items():
            row = await session.get(Setting, key)
            if row is not None and row.value == old_default:
                row.value = new_default
        session.add(Setting(key=MIGRATION_FLAG, value=True))
    await session.flush()

    count = await session.scalar(select(func.count(User.id)).where(User.role == UserRole.ADMIN.value))
    if count and count > 0:
        return

    password = settings.admin_password
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True

    admin = User(
        username=settings.admin_username,
        password_hash=hash_password(password),
        role=UserRole.ADMIN.value,
        agreement_version=int(DEFAULT_SETTINGS.get("agreement_version") or 1),  # 运维者创建即视为已同意
    )
    session.add(admin)
    await session.flush()
    # 管理员默认不限额度
    session.add_all(
        [UserQuota(user_id=admin.id, feature=f, quota_total=-1, quota_used=0) for f in ("chat", "image", "search", "ppt")]
    )
    logger.warning("=" * 60)
    logger.warning("已创建初始管理员账号：%s", settings.admin_username)
    if generated:
        logger.warning("未配置 ADMIN_PASSWORD，生成的临时密码（仅显示一次）：%s", password)
    logger.warning("请登录后立即在“个人中心”修改密码。")
    logger.warning("=" * 60)
