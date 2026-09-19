"""管理端：总览统计、调用日志、审计日志。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import FEATURE_LABELS, AuditLog, UsageLog, User
from ..services import stats

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    return await stats.overview(db)


@router.get("/logs")
async def usage_logs(
    username: str = Query(default=""),
    feature: str = Query(default=""),
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    q = select(UsageLog, User.username).join(User, User.id == UsageLog.user_id)
    count_q = select(func.count(UsageLog.id))
    if username:
        q = q.where(User.username.contains(username))
        count_q = count_q.join(User, User.id == UsageLog.user_id).where(User.username.contains(username))
    if feature:
        q = q.where(UsageLog.feature == feature)
        count_q = count_q.where(UsageLog.feature == feature)
    if status:
        q = q.where(UsageLog.status == status)
        count_q = count_q.where(UsageLog.status == status)
    total = await db.scalar(count_q) or 0
    result = await db.execute(
        q.order_by(UsageLog.created_at.desc()).offset((page - 1) * size).limit(size)
    )
    return {
        "total": total,
        "items": [
            {
                "id": log.id,
                "username": name,
                "user_id": log.user_id,
                "feature": log.feature,
                "feature_label": FEATURE_LABELS.get(log.feature, log.feature),
                "model": log.model,
                "endpoint": log.endpoint,
                "status": log.status,
                "error": log.error,
                "latency_ms": log.latency_ms,
                "units": log.units,
                "prompt_tokens": log.prompt_tokens,
                "completion_tokens": log.completion_tokens,
                "ip": log.ip,
                "created_at": log.created_at,
            }
            for log, name in result.all()
        ],
    }


@router.get("/audit-logs")
async def audit_logs(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    total = await db.scalar(select(func.count(AuditLog.id))) or 0
    result = await db.execute(
        select(AuditLog, User.username)
        .join(User, User.id == AuditLog.admin_id)
        .order_by(AuditLog.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return {
        "total": total,
        "items": [
            {
                "id": log.id,
                "admin": name,
                "action": log.action,
                "target": log.target,
                "detail": log.detail,
                "created_at": log.created_at,
            }
            for log, name in result.all()
        ],
    }
