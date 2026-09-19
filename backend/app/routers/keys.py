"""用户 API 密钥（newapi 风格，明文仅在创建时展示一次）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import get_current_user
from ..models import ApiKey, User
from ..schemas import ApiKeyCreateIn
from ..security import generate_api_key

router = APIRouter(prefix="/api/keys", tags=["keys"])


def _key_out(row: ApiKey) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "enabled": row.enabled,
        "last_used_at": row.last_used_at,
        "created_at": row.created_at,
    }


@router.get("")
async def list_keys(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    return {"items": [_key_out(r) for r in result.scalars()]}


@router.post("")
async def create_key(body: ApiKeyCreateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    count_result = await db.execute(select(ApiKey).where(ApiKey.user_id == user.id))
    if len(count_result.scalars().all()) >= 20:
        raise HTTPException(status_code=400, detail="每个用户最多创建 20 个密钥")
    raw, prefix, key_hash = generate_api_key()
    row = ApiKey(user_id=user.id, name=body.name or "默认密钥", prefix=prefix, key_hash=key_hash)
    db.add(row)
    await db.commit()
    return {"key": raw, "item": _key_out(row)}


@router.patch("/{key_id}")
async def toggle_key(key_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="密钥不存在")
    row.enabled = not row.enabled
    await db.commit()
    return _key_out(row)


@router.delete("/{key_id}")
async def delete_key(key_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="密钥不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}
