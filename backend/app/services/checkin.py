"""每日签到。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from .quota import compute_temp_expiry, grant, safe_tz
from .settings_store import get_setting


async def today_key(db: AsyncSession) -> str:
    tz_name = str(await get_setting(db, "checkin_timezone") or "Asia/Shanghai")
    return datetime.now(safe_tz(tz_name)).strftime("%Y-%m-%d")


async def has_checked_in_today(db: AsyncSession, user: User) -> bool:
    return bool(user.last_checkin_key) and user.last_checkin_key == await today_key(db)


async def do_checkin(db: AsyncSession, user: User) -> dict:
    """执行签到，返回 {feature: amount} 发放明细；已签到则抛 ValueError。"""
    tz_name = str(await get_setting(db, "checkin_timezone") or "Asia/Shanghai")
    key = datetime.now(safe_tz(tz_name)).strftime("%Y-%m-%d")
    # 原子占位：并发双击/多标签只有一个请求能签到成功
    from sqlalchemy import update

    result = await db.execute(
        update(User).where(User.id == user.id, User.last_checkin_key != key).values(last_checkin_key=key)
    )
    if result.rowcount != 1:
        raise ValueError("今天已经签到过了，明天再来吧")
    user.last_checkin_key = key

    pool = str(await get_setting(db, "checkin_pool") or "permanent")
    days = int(await get_setting(db, "checkin_valid_days") or 1)
    hours = int(await get_setting(db, "checkin_valid_hours") or 0)
    amounts = {
        "chat": int(await get_setting(db, "checkin_chat") or 0),
        "image": int(await get_setting(db, "checkin_image") or 0),
        "search": int(await get_setting(db, "checkin_search") or 0),
        "ppt": int(await get_setting(db, "checkin_ppt") or 0),
    }
    granted: dict[str, int] = {}
    expires_at = compute_temp_expiry(tz_name, days, hours) if pool == "temporary" else None
    for feature, amount in amounts.items():
        if amount > 0:
            await grant(db, user.id, feature, amount, pool=pool, expires_at=expires_at)
            granted[feature] = amount
    await db.commit()
    return granted
