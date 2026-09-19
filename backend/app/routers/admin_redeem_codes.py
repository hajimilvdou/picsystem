"""管理端：兑换码（纯额度，老用户兑换）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import RedemptionCode, User
from ..schemas import InviteCreateIn, InvitePatchIn
from ..security import generate_invite_code
from ..services.usage import audit

router = APIRouter(prefix="/api/admin/redeem-codes", tags=["admin-redeem-codes"])


def _out(row: RedemptionCode) -> dict:
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
async def list_codes(
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    query = select(RedemptionCode)
    count_q = select(func.count(RedemptionCode.id))
    if q:
        query = query.where(RedemptionCode.code.contains(q))
        count_q = count_q.where(RedemptionCode.code.contains(q))
    total = await db.scalar(count_q) or 0
    result = await db.execute(
        query.order_by(RedemptionCode.id.desc()).offset((page - 1) * size).limit(size)
    )
    return {"total": total, "items": [_out(r) for r in result.scalars()]}


@router.post("")
async def create_code(
    body: InviteCreateIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = RedemptionCode(
        code=body.code or generate_invite_code(),
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
        raise HTTPException(status_code=409, detail="兑换码已存在") from None
    await audit(db, admin_id=admin.id, action="redeem_code.create", target=row.code,
                detail=f"max_uses={body.max_uses}")
    return _out(row)


@router.patch("/{code_id}")
async def update_code(
    code_id: int,
    body: InvitePatchIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = await db.get(RedemptionCode, code_id)
    if row is None:
        raise HTTPException(status_code=404, detail="兑换码不存在")
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
    if "expires_at" in body.model_fields_set and row.expires_at != body.expires_at:
        row.expires_at = body.expires_at
        changes.append("expires_at")
    if "fixed_expires_at" in body.model_fields_set and row.fixed_expires_at != body.fixed_expires_at:
        row.fixed_expires_at = body.fixed_expires_at
        changes.append("fixed_expires_at")
    await db.commit()
    if changes:
        await audit(db, admin_id=admin.id, action="redeem_code.update", target=row.code,
                    detail=",".join(changes))
    return _out(row)


@router.delete("/{code_id}")
async def delete_code(
    code_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = await db.get(RedemptionCode, code_id)
    if row is None:
        raise HTTPException(status_code=404, detail="兑换码不存在")
    code = row.code
    await db.delete(row)
    await db.commit()
    await audit(db, admin_id=admin.id, action="redeem_code.delete", target=code)
    return {"ok": True}
