"""管理端：风控中心（事件、锁定名单、解锁）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import RiskEvent, User
from ..services.risk import login_protector
from ..services.usage import audit

router = APIRouter(prefix="/api/admin/risk", tags=["admin-risk"], dependencies=[Depends(require_admin)])

KIND_LABELS = {
    "login_fail": "登录失败",
    "login_locked": "触发锁定",
    "rate_limit": "触发限流",
    "register": "注册",
    "register_blocked": "注册拦截",
    "agreement_accept": "同意协议",
    "redeem": "兑换",
    "content_blocked": "内容拦截",
}


@router.get("/events")
async def list_events(
    kind: str = Query(default=""),
    username: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    q = select(RiskEvent)
    count_q = select(func.count(RiskEvent.id))
    if kind:
        q = q.where(RiskEvent.kind == kind)
        count_q = count_q.where(RiskEvent.kind == kind)
    if username:
        q = q.where(RiskEvent.username.contains(username))
        count_q = count_q.where(RiskEvent.username.contains(username))
    total = await db.scalar(count_q) or 0
    result = await db.execute(
        q.order_by(RiskEvent.created_at.desc()).offset((page - 1) * size).limit(size)
    )
    return {
        "total": total,
        "kind_labels": KIND_LABELS,
        "items": [
            {
                "id": r.id,
                "kind": r.kind,
                "kind_label": KIND_LABELS.get(r.kind, r.kind),
                "username": r.username,
                "ip": r.ip,
                "detail": r.detail,
                "created_at": r.created_at,
            }
            for r in result.scalars()
        ],
    }


@router.get("/locks")
async def list_locks():
    return {"items": await login_protector.list_locks()}


class UnlockIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    ip: str = Field(default="", max_length=64)


@router.post("/unlock")
async def unlock(body: UnlockIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    if not body.ip:
        raise HTTPException(status_code=400, detail="缺少 ip 参数")
    remaining = await login_protector.locked_remaining(body.username, body.ip)
    if not remaining:
        raise HTTPException(status_code=404, detail="该用户名与 IP 组合当前没有生效中的锁定")
    await login_protector.clear(body.username, body.ip)
    await audit(db, admin_id=admin.id, action="risk.unlock", target=f"{body.username}@{body.ip}")
    return {"ok": True}
