"""额度服务：双池模型。

- 永久组（quota_total/quota_used）：quota_total=-1 表示不限
- 限时组（temp_amount/temp_expires_at）：到期前优先消耗，过期自动作废
- 消耗顺序：限时组 → 永久组；退还按"先还永久组已用、余量回限时组"
- UsageLog 已含 token 与 cost 字段，便于将来扩展 token 计费与价格体系
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UserQuota


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def safe_tz(tz_name: str):
    """时区解析：非法名称或未装 tzdata 时回退到 UTC+8（与默认时区一致）。"""
    try:
        return ZoneInfo(tz_name)
    except Exception:
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            return timezone(timedelta(hours=8))


def compute_temp_expiry(tz_name: str, valid_days: int, valid_hours: int) -> datetime:
    """限时额度到期时间：时区内 (今天+valid_days) 00:00 + valid_hours 小时。

    例：valid_days=1, valid_hours=0 → 当天 24:00（即次日 0 点）。
    """
    tz = safe_tz(tz_name)
    now_local = datetime.now(tz)
    base = (now_local + timedelta(days=max(valid_days, 1))).replace(hour=0, minute=0, second=0, microsecond=0)
    return (base + timedelta(hours=max(valid_hours, 0))).astimezone(timezone.utc)


def fmt_temp_expiry(dt: datetime | None) -> str:
    if not dt:
        return ""
    dt = _aware(dt)
    return dt.astimezone(safe_tz("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


async def get_quotas(session: AsyncSession, user_id: int) -> dict[str, dict]:
    result = await session.execute(
        UserQuota.__table__.select().where(UserQuota.user_id == user_id)
    )
    now = utcnow()
    quotas: dict[str, dict] = {}
    for row in result:
        temp_valid = bool(
            row.temp_amount > 0 and _aware(row.temp_expires_at) and _aware(row.temp_expires_at) > now
        )
        quotas[row.feature] = {
            "total": row.quota_total,
            "used": row.quota_used,
            "temp": row.temp_amount if temp_valid else 0,
            "temp_expires_at": _aware(row.temp_expires_at).isoformat() if temp_valid else None,
        }
    for feature in ("chat", "image", "search", "ppt"):
        quotas.setdefault(feature, {"total": 0, "used": 0, "temp": 0, "temp_expires_at": None})
    return quotas


async def remaining(session: AsyncSession, user_id: int, feature: str) -> int:
    """返回剩余次数；-1 表示不限。"""
    row = await session.get(UserQuota, {"user_id": user_id, "feature": feature})
    if row is None:
        return 0
    if row.quota_total < 0:
        return -1
    temp = 0
    if row.temp_amount > 0 and _aware(row.temp_expires_at) and _aware(row.temp_expires_at) > utcnow():
        temp = row.temp_amount
    return max(row.quota_total - row.quota_used, 0) + temp


async def consume(session: AsyncSession, user_id: int, feature: str, units: int = 1) -> tuple[bool, int, int]:
    """原子预扣（限时组优先）。返回 (是否成功, 限时组扣量, 永久组扣量)，
    失败时调用方按拆分 refund(take_temp, take_perm) 原路退回。"""
    if units <= 0:
        return True, 0, 0
    for _ in range(2):  # 竞争时按最新值重试一次
        row = await session.get(UserQuota, {"user_id": user_id, "feature": feature})
        if row is None:
            return False, 0, 0
        expires = _aware(row.temp_expires_at)
        temp_available = row.temp_amount if (expires and expires > utcnow()) else 0
        take_temp = min(temp_available, units)
        take_perm = units - take_temp
        stmt = (
            update(UserQuota)
            .where(UserQuota.user_id == user_id)
            .where(UserQuota.feature == feature)
            .where(UserQuota.temp_amount >= take_temp)
            .where((UserQuota.quota_total < 0) | (UserQuota.quota_used + take_perm <= UserQuota.quota_total))
            .values(temp_amount=UserQuota.temp_amount - take_temp, quota_used=UserQuota.quota_used + take_perm)
            .execution_options(synchronize_session=False)
        )
        if take_temp > 0:
            stmt = stmt.where(UserQuota.temp_expires_at > utcnow())
        result = await session.execute(stmt)
        if result.rowcount == 1:
            await session.commit()
            # synchronize_session=False 后手动同步内存对象，供同会话后续读取
            row.temp_amount -= take_temp
            row.quota_used += take_perm
            return True, take_temp, take_perm
        await session.rollback()
    return False, 0, 0


async def refund(session: AsyncSession, user_id: int, feature: str, units: int = 1,
                 take_temp: int = 0, take_perm: int = 0) -> None:
    """按 consume 返回的拆分原路退回；未传拆分时退回永久组已用（旧行为）。"""
    if units <= 0:
        return
    row = await session.get(UserQuota, {"user_id": user_id, "feature": feature})
    if row is None:
        return
    if take_temp + take_perm != units:
        take_temp, take_perm = 0, min(row.quota_used, units)
    values: dict = {
        "quota_used": case((UserQuota.quota_used - take_perm > 0, UserQuota.quota_used - take_perm), else_=0)
    }
    if take_temp > 0:
        values["temp_amount"] = UserQuota.temp_amount + take_temp
        if row.temp_expires_at is None:
            values["temp_expires_at"] = utcnow() + timedelta(hours=24)
    stmt = (
        update(UserQuota)
        .where(UserQuota.user_id == user_id)
        .where(UserQuota.feature == feature)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.execute(stmt)
    await session.commit()
    row.quota_used = max(row.quota_used - take_perm, 0)
    row.temp_amount += take_temp


async def grant(
    session: AsyncSession,
    user_id: int,
    feature: str,
    amount: int,
    *,
    pool: str = "permanent",
    expires_at: datetime | None = None,
    commit: bool = True,
) -> bool:
    """发放额度：permanent 进永久组（增加 total）；temporary 进限时组（累加并取更晚到期）。

    commit=False 时由调用方统一提交（用于一次操作发放多项额度的原子性）。
    到期时间已过的限时发放直接忽略，返回 False。
    """
    if pool == "temporary" and expires_at is not None and _aware(expires_at) <= utcnow():
        if commit:
            await session.commit()
        return False
    row = await session.get(UserQuota, {"user_id": user_id, "feature": feature})
    if row is None:
        row = UserQuota(user_id=user_id, feature=feature)
        session.add(row)
        await session.flush()
    if pool == "temporary" and expires_at is not None:
        row.temp_amount += amount
        current = _aware(row.temp_expires_at)
        row.temp_expires_at = max(current, expires_at) if current else expires_at
    else:
        if row.quota_total >= 0:
            row.quota_total += amount
    if commit:
        await session.commit()
    return True
