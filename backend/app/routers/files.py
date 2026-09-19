"""用户文件：列表 / 下载 / 删除。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import get_current_user
from ..models import StoredFile, User
from ..services.storage import delete_file, resolve_path

router = APIRouter(prefix="/api/files", tags=["files"])


def _file_out(row: StoredFile) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "filename": row.filename,
        "mime": row.mime,
        "size": row.size,
        "prompt": row.prompt,
        "url": f"/api/files/{row.id}/download",
        "created_at": row.created_at,
    }


@router.get("")
async def list_files(
    kind: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=24, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(StoredFile).where(StoredFile.user_id == user.id)
    count_q = select(func.count(StoredFile.id)).where(StoredFile.user_id == user.id)
    if kind:
        q = q.where(StoredFile.kind == kind)
        count_q = count_q.where(StoredFile.kind == kind)
    total = await db.scalar(count_q) or 0
    result = await db.execute(
        q.order_by(StoredFile.created_at.desc()).offset((page - 1) * size).limit(size)
    )
    return {"total": total, "items": [_file_out(r) for r in result.scalars()]}


async def _get_own_file(db: AsyncSession, user_id: int, file_id: int) -> StoredFile:
    row = await db.get(StoredFile, file_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    return row


@router.get("/{file_id}/download")
async def download(file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await _get_own_file(db, user.id, file_id)
    try:
        path = resolve_path(row.path)
    except PermissionError:
        raise HTTPException(status_code=404, detail="文件不存在") from None
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已被清理")
    inline_types = {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
    disposition = "inline" if row.mime in inline_types else "attachment"
    return FileResponse(
        path,
        media_type=row.mime,
        filename=row.filename,
        content_disposition_type=disposition,
    )


@router.delete("/{file_id}")
async def remove(file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await _get_own_file(db, user.id, file_id)
    delete_file(row.path)
    await db.delete(row)
    await db.commit()
    return {"ok": True}
