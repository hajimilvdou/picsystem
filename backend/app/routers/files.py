"""用户文件：列表 / 下载 / 缩略图 / 压缩 / 打包下载 / 删除 / 标签。"""
from __future__ import annotations

import os
import re
import tempfile
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import get_current_user
from ..models import StoredFile, User
from ..services.image_compress import (
    COMPRESSIBLE_MIME,
    DEFAULT_QUALITY,
    MAX_MAX_EDGE,
    MAX_QUALITY,
    MAX_SOURCE_BYTES as MAX_COMPRESS_SOURCE_BYTES,
    MIN_MAX_EDGE,
    MIN_QUALITY,
    OUTPUT_MIME,
    CompressError,
    compress,
    output_filename,
    output_rel_path,
)
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
    # v=size 当缓存键：文件被压缩替换后 size 会变，浏览器自然会重新拉缩略图
    thumb_url = f"/api/files/{row.id}/thumb?v={row.size}" if row.mime in THUMBABLE_MIME else None
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


ARCHIVE_MAX_FILES = 300
ARCHIVE_MAX_BYTES = 1024 * 1024 * 1024  # 单次打包的原始体积上限（1GB）


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        # 18 位以内：64 位整数最多 19 位，超长数字交给数据库会直接报错（500）
        if not part.isdigit() or len(part) > 18:
            raise HTTPException(status_code=400, detail="ids 只能是以逗号分隔的文件编号")
        ids.append(int(part))
    if not ids:
        raise HTTPException(status_code=400, detail="请先选择要打包的文件")
    if len(ids) > ARCHIVE_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"单次最多打包 {ARCHIVE_MAX_FILES} 个文件")
    return list(dict.fromkeys(ids))


def _zip_entry_name(name: str, used: set[str]) -> str:
    """生成 zip 内的安全文件名：去掉路径分隔符、重名自动加序号。"""
    safe = re.sub(r"[\\/]+", "_", str(name or "file")).lstrip(".") or "file"
    if safe not in used:
        used.add(safe)
        return safe
    stem, dot, suffix = safe.rpartition(".")
    base, ext = (stem, f".{suffix}") if dot else (safe, "")
    index = 2
    while f"{base} ({index}){ext}" in used:
        index += 1
    final = f"{base} ({index}){ext}"
    used.add(final)
    return final


def _build_archive(rows: list[StoredFile]) -> str:
    """把产物写进临时 zip，返回临时文件路径（调用方负责在响应结束后删除）。"""
    handle = tempfile.NamedTemporaryFile(prefix="picsystem-", suffix=".zip", delete=False)
    handle.close()
    used: set[str] = set()
    with zipfile.ZipFile(handle.name, "w", zipfile.ZIP_STORED) as archive:
        for row in rows:
            try:
                path = resolve_path(row.path)
            except PermissionError:
                continue
            if not path.exists():
                continue
            archive.write(path, _zip_entry_name(row.filename, used))
    return handle.name


class CompressIn(BaseModel):
    """压缩参数。dry_run 默认为真：不传 false 就只做预估，不会动原文件。"""

    quality: int = Field(default=DEFAULT_QUALITY, ge=MIN_QUALITY, le=MAX_QUALITY)
    max_edge: int | None = Field(default=None, ge=MIN_MAX_EDGE, le=MAX_MAX_EDGE)
    dry_run: bool = True


@router.post("/{file_id}/compress")
async def compress_file(
    file_id: int,
    body: CompressIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把图片压缩为 WebP（**不可逆**：会替换原文件）。

    dry_run=True（默认）只返回预估大小，不写盘；前端展示预估、用户确认后再传 false。
    """
    row = await _get_own_file(db, user.id, file_id)
    if row.mime not in COMPRESSIBLE_MIME:
        raise HTTPException(
            status_code=400,
            detail="该文件类型不支持压缩（GIF 重编码会丢动画，PPT / PSD / PDF 不处理）",
        )
    try:
        source = resolve_path(row.path)
    except PermissionError:
        raise HTTPException(status_code=404, detail="文件不存在") from None
    if not source.exists():
        raise HTTPException(status_code=404, detail="文件已被清理")
    source_bytes = source.stat().st_size
    before = int(row.size or 0) or source_bytes
    if source_bytes > MAX_COMPRESS_SOURCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"原图超过 {MAX_COMPRESS_SOURCE_BYTES // 1048576}MB，不压缩",
        )

    try:
        result = await run_in_threadpool(compress, source, quality=body.quality, max_edge=body.max_edge)
    except CompressError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    after = result.size
    payload = {
        "id": row.id,
        "dry_run": body.dry_run,
        "before_bytes": before,
        "after_bytes": after,
        "saved_bytes": max(0, before - after),
        "saved_ratio": round(max(0, before - after) / before, 4) if before else 0.0,
        "width": result.width,
        "height": result.height,
        "quality": body.quality,
    }
    if body.dry_run:
        payload["worthwhile"] = after < before
        return payload

    if after >= before:
        payload["ok"] = False
        payload["skipped_reason"] = "压缩后体积反而更大，已跳过（原文件未改动）"
        return payload

    old_rel = row.path
    new_rel = output_rel_path(old_rel)
    try:
        target = resolve_path(new_rel)
    except PermissionError:
        raise HTTPException(status_code=500, detail="目标路径非法") from None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_bytes(result.content)
        tmp.replace(target)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入压缩结果失败：{exc}") from exc

    # 原文件与它的缩略图一起清理；新路径的缩略图会在下次访问时按需重建
    delete_file(old_rel)
    row.path = new_rel
    row.mime = OUTPUT_MIME
    row.filename = output_filename(row.filename)
    row.size = after
    await db.commit()
    payload["ok"] = True
    payload["mime"] = OUTPUT_MIME
    payload["filename"] = row.filename
    return payload


@router.get("/archive")
async def archive(
    ids: str = Query(default="", description="逗号分隔的文件 id"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把选中的产物打包成 zip 下载（只读操作，不动原件）。

    用 GET + 逗号分隔 id：前端可以直接用 `<a download>` 触发浏览器原生下载，
    不必把整个 zip 读进内存变成 blob（几百 MB 的包在低配客户端上会爆内存）。
    实际体积上限由前端预检 + 后端兜底双重把关。
    """
    wanted = _parse_ids(ids)
    result = await db.execute(
        select(StoredFile).where(StoredFile.user_id == user.id, StoredFile.id.in_(wanted))
    )
    found = {row.id: row for row in result.scalars()}
    rows = [found[file_id] for file_id in wanted if file_id in found]  # 保持选择顺序
    if not rows:
        raise HTTPException(status_code=404, detail="没有可打包的文件")
    total = sum(int(row.size or 0) for row in rows)
    if total > ARCHIVE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"选中的文件合计 {total / 1048576:.0f}MB，超过单次打包上限 "
                   f"{ARCHIVE_MAX_BYTES // 1048576}MB，请分批下载",
        )
    zip_path = await run_in_threadpool(_build_archive, rows)
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=500, detail="打包失败")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"picsystem-{stamp}.zip",
        # 响应结束后立刻删掉临时包，避免堆积
        background=BackgroundTask(lambda: os.unlink(zip_path) if os.path.exists(zip_path) else None),
    )


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
