"""额度发放逻辑（邀请码注册初始额度与兑换码兑换共用）。"""
from __future__ import annotations

from datetime import timezone

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import InviteCode, RedemptionCode, User, UserQuota
from .quota import compute_temp_expiry, grant
from .settings_store import get_setting

FEATURE_AMOUNTS = ("chat", "image", "search", "ppt")


def invite_amounts(invite: InviteCode | RedemptionCode) -> dict[str, int]:
    return {
        "chat": invite.chat_quota,
        "image": invite.image_quota,
        "search": invite.search_quota,
        "ppt": invite.ppt_quota,
    }


async def apply_invite_grants(db: AsyncSession, user: User, invite: InviteCode | RedemptionCode) -> dict[str, int]:
    """按码的池类型向用户发放额度，返回实际发放明细（一次操作统一提交，保证原子性）。

    限时到期规则：设置了 fixed_expires_at（固定到期时间）时所有人统一在该
    时间点失效；否则按「有效天数 + 附加小时」相对发放日计算。
    """
    tz_name = str(await get_setting(db, "checkin_timezone") or "Asia/Shanghai")
    if invite.pool_type == "temporary":
        if invite.fixed_expires_at is not None:
            expires_at = invite.fixed_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = compute_temp_expiry(tz_name, invite.valid_days, invite.valid_hours)
    else:
        expires_at = None
    granted: dict[str, int] = {}
    for feature, amount in invite_amounts(invite).items():
        if amount == 0:
            continue
        if invite.pool_type == "temporary" and amount > 0:
            applied = await grant(
                db, user.id, feature, amount, pool="temporary",
                expires_at=expires_at, commit=False,
            )
            if applied:
                granted[feature] = amount
        else:
            row = await db.get(UserQuota, {"user_id": user.id, "feature": feature})
            if row is None:
                row = UserQuota(user_id=user.id, feature=feature, quota_total=0)
                db.add(row)
                await db.flush()
            if row.quota_total >= 0:  # 不限额账号不受影响
                row.quota_total += amount
                granted[feature] = amount
    await db.commit()
    return granted
