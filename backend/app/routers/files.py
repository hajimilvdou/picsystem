"""用户文件：列表 / 下载 / 删除 / 标签。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import get_current_user
from ..models import StoredFile, User
from ..services.storage import delete_file, resolve_path
from ..services.thumbnails import THUMBABLE_MIME, ensure_thumbnail
from ..services.tags import (
    MAX_TAGS_PER_FILE,
    column_to_tags,
    normalize_tags,
    tag_filter,
    tags_to_column,
)

router = APIRouter(prefix="/api/files", tags=["files"])

# 标签聚合最多扫描多少行：个人图库足够，避免超大库拖慢接口
TAG_SCAN_LIMIT = 2000


def _file_out(row: StoredFile) -> dict:
    # 图片类才给缩略图地址；取不到时前端回退到原图
    thumb_url = f"/api/files/{row.id}/thumb" if row.mime in THUMBABLE_MIME else None
    return {
        "id": row.id,
        "kind": row.kind,
        "filename": row.filename,
        "mime": row.mime,
        "size": row.size,
        "prompt": row.prompt,
        "tags": column_to_tags(row.tags),
        "url": f"/api/files/{row.id}/download",
        "thumb_url": thumb_url,
        "created_at": row.created_at,
    }


@router.get("")
async def list_files(
    kind: str = Query(default=""),
    tag: str = Query(default=""),
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
    clean = normalize_tags([tag])
    if clean:
        condition = tag_filter(StoredFile.tags, clean[0])
        q = q.where(condition)
        count_q = count_q.where(condition)
    total = await db.scalar(count_q) or 0
    result = await db.execute(
        q.order_by(StoredFile.created_at.desc()).offset((page - 1) * size).limit(size)
    )
    return {"total": total, "items": [_file_out(r) for r in result.scalars()]}


@router.get("/tags")
async def list_tags(
    kind: str = Query(default=""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户的标签聚合（用于筛选与补全），按使用次数降序。"""
    q = select(StoredFile.tags).where(StoredFile.user_id == user.id)
    if kind:
        q = q.where(StoredFile.kind == kind)
    rows = (await db.execute(q.order_by(StoredFile.created_at.desc()).limit(TAG_SCAN_LIMIT))).all()
    counts: dict[str, int] = {}
    for (raw,) in rows:
        for name in column_to_tags(raw):
            counts[name] = counts.get(name, 0) + 1
    items = [
        {"tag": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {"items": items, "scanned": len(rows), "limit": TAG_SCAN_LIMIT}


class TagsIn(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS_PER_FILE * 4)


async def _get_own_file(db: AsyncSession, user_id: int, file_id: int) -> StoredFile:
    row = await db.get(StoredFile, file_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    return row


@router.patch("/{file_id}")
async def update_tags(
    file_id: int,
    body: TagsIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """整组覆盖式设置标签（传空数组即清空）。"""
    row = await _get_own_file(db, user.id, file_id)
    tags = normalize_tags(body.tags)
    row.tags = tags_to_column(tags)
    await db.commit()
    return {"ok": True, "id": row.id, "tags": tags}


@router.get("/{file_id}/thumb")
async def thumbnail(file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """缩略图（长边 400px 的 JPEG）。首次请求时生成并落盘缓存，失败则 404 让前端回退原图。"""
    row = await _get_own_file(db, user.id, file_id)
    if row.mime not in THUMBABLE_MIME:
        raise HTTPException(status_code=404, detail="该文件没有缩略图")
    try:
        source = resolve_path(row.path)
    except PermissionError:
        raise HTTPException(status_code=404, detail="文件不存在") from None
    # 解码属于 CPU 密集操作，放到线程池，避免阻塞事件循环
    dest = await run_in_threadpool(ensure_thumbnail, source, row.path)
    if dest is None:
        raise HTTPException(status_code=404, detail="缩略图不可用")
    return FileResponse(
        dest,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=604800"},
    )


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
