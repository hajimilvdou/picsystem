"""管理端统计聚合。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import inflight
from ..models import FEATURE_LABELS, UsageLog, User


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def overview(session: AsyncSession) -> dict:
    today = _day_start()
    week_ago = today - timedelta(days=6)

    total_users = await session.scalar(select(func.count(User.id))) or 0
    pending_users = await session.scalar(
        select(func.count(User.id)).where(User.status == "pending")
    ) or 0

    today_q = await session.execute(
        select(
            func.count(UsageLog.id),
            func.coalesce(func.sum(case((UsageLog.status == "success", 1), else_=0)), 0),
            func.coalesce(func.sum(case((UsageLog.feature == "image", UsageLog.units), else_=0)), 0),
        ).where(UsageLog.created_at >= today)
    )
    today_total, today_success, today_images = today_q.one()

    total_requests = await session.scalar(select(func.count(UsageLog.id))) or 0

    # 7 天趋势（按天 + 功能）
    trend_rows = await session.execute(
        select(
            func.date(UsageLog.created_at).label("day"),
            UsageLog.feature,
            func.count(UsageLog.id),
        )
        .where(UsageLog.created_at >= week_ago)
        .group_by("day", UsageLog.feature)
        .order_by("day")
    )
    trend: dict[str, dict[str, int]] = {}
    for i in range(7):
        day = (week_ago + timedelta(days=i)).date().isoformat()
        trend[day] = {f: 0 for f in FEATURE_LABELS}
    for day, feature, count in trend_rows.all():
        day_str = str(day)
        trend.setdefault(day_str, {f: 0 for f in FEATURE_LABELS})
        if feature in FEATURE_LABELS:
            trend[day_str][feature] = count

    # 功能分布（近 30 天）
    month_ago = today - timedelta(days=29)
    dist_rows = await session.execute(
        select(UsageLog.feature, func.count(UsageLog.id))
        .where(UsageLog.created_at >= month_ago)
        .group_by(UsageLog.feature)
    )
    feature_distribution = [
        {"feature": f, "label": FEATURE_LABELS.get(f, f), "count": c} for f, c in dist_rows.all()
    ]

    # 活跃用户 TOP（近 7 天）
    top_rows = await session.execute(
        select(User.username, func.count(UsageLog.id).label("cnt"))
        .join(UsageLog, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= week_ago)
        .group_by(User.username)
        .order_by(func.count(UsageLog.id).desc())
        .limit(8)
    )
    top_users = [{"username": u, "count": c} for u, c in top_rows.all()]

    active = await inflight.total()

    return {
        "total_users": total_users,
        "pending_users": pending_users,
        "total_requests": total_requests,
        "today_requests": today_total,
        "today_success_rate": round(today_success / today_total * 100, 1) if today_total else 100.0,
        "today_images": int(today_images or 0),
        "active_requests": active,
        "trend": [{"day": d, **counts} for d, counts in sorted(trend.items())],
        "feature_distribution": feature_distribution,
        "top_users": top_users,
    }
