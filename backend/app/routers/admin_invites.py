"""管理端：邀请码。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import InviteCode, User
from ..schemas import InviteCreateIn, InvitePatchIn
from ..security import generate_invite_code
from ..services.usage import audit

router = APIRouter(prefix="/api/admin/invites", tags=["admin-invites"])


def _invite_out(row: InviteCode) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "note": row.note,
        "max_uses": row.max_uses,
        "used_count": row.used_count,
        "quotas": {
            "chat": row.chat_quota,
            "image": row.image_quota,
            "search": row.search_quota,
            "ppt": row.ppt_quota,
        },
        "pool_type": row.pool_type,
        "valid_days": row.valid_days,
        "valid_hours": row.valid_hours,
        "fixed_expires_at": row.fixed_expires_at,
        "enabled": row.enabled,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
    }


@router.get("")
async def list_invites(
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    query = select(InviteCode)
    count_q = select(func.count(InviteCode.id))
    if q:
        query = query.where(InviteCode.code.contains(q))
        count_q = count_q.where(InviteCode.code.contains(q))
    total = await db.scalar(count_q) or 0
    result = await db.execute(
        query.order_by(InviteCode.id.desc()).offset((page - 1) * size).limit(size)
    )
    return {"total": total, "items": [_invite_out(r) for r in result.scalars()]}


@router.post("")
async def create_invite(
    body: InviteCreateIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    code = body.code or generate_invite_code()
    row = InviteCode(
        code=code,
        note=body.note,
        max_uses=body.max_uses,
        chat_quota=body.chat_quota,
        image_quota=body.image_quota,
        search_quota=body.search_quota,
        ppt_quota=body.ppt_quota,
        pool_type=body.pool_type,
        valid_days=body.valid_days,
        valid_hours=body.valid_hours,
        fixed_expires_at=body.fixed_expires_at,
        expires_at=body.expires_at,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="邀请码已存在") from None
    await audit(db, admin_id=admin.id, action="invite.create", target=code,
                detail=f"max_uses={body.max_uses}")
    return _invite_out(row)


@router.patch("/{invite_id}")
async def update_invite(
    invite_id: int,
    body: InvitePatchIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = await db.get(InviteCode, invite_id)
    if row is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    changes = []
    for field, column in (
        ("note", "note"), ("max_uses", "max_uses"), ("enabled", "enabled"),
        ("chat_quota", "chat_quota"), ("image_quota", "image_quota"),
        ("search_quota", "search_quota"), ("ppt_quota", "ppt_quota"),
        ("pool_type", "pool_type"), ("valid_days", "valid_days"), ("valid_hours", "valid_hours"),
    ):
        value = getattr(body, field)
        if value is not None and getattr(row, column) != value:
            setattr(row, column, value)
            changes.append(field)
    # 有效期允许显式置空（清除过期时间）
    if "expires_at" in body.model_fields_set and row.expires_at != body.expires_at:
        row.expires_at = body.expires_at
        changes.append("expires_at")
    if "fixed_expires_at" in body.model_fields_set and row.fixed_expires_at != body.fixed_expires_at:
        row.fixed_expires_at = body.fixed_expires_at
        changes.append("fixed_expires_at")
    await db.commit()
    if changes:
        await audit(db, admin_id=admin.id, action="invite.update", target=row.code,
                    detail=",".join(changes))
    return _invite_out(row)


@router.delete("/{invite_id}")
async def delete_invite(
    invite_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = await db.get(InviteCode, invite_id)
    if row is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    code = row.code
    await db.delete(row)
    await db.commit()
    await audit(db, admin_id=admin.id, action="invite.delete", target=code)
    return {"ok": True}
