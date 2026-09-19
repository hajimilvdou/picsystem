"""绘图：文生图 / 图生图，结果落本地图库。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from ..config import settings
from ..database import get_db
from ..deps import client_ip, get_current_user, inflight, inflight_limit, user_rate_limit
from ..models import User
from ..schemas import ImageGenIn
from ..services.content_guard import check_content
from ..services.guard import require_feature
from ..services.image_inputs import (
    IMAGE_REFERENCE_FIELDS,
    MASK_REFERENCE_FIELDS,
    collect_uploads,
)
from ..services.image_results import persist_image_results
from ..services.quota import consume, refund
from ..services.storage import StorageFullError
from ..services.upstream import UpstreamError, request_json
from ..services.usage import log_usage

router = APIRouter(prefix="/api/images", tags=["images"])

MAX_REFERENCE_IMAGES = 4
# 转发给上游时使用的字段名（不透明区域 = 需要重绘的部分，与上游 mask 口径一致）。
# 注意区分：入参接受 mask / mask[] 多个别名（见 services/image_inputs.py），出参统一用 mask。
MASK_FIELD = "mask"


def _file_out(row) -> dict:
    return {
        "id": row.id,
        "url": f"/api/files/{row.id}/download",
        "thumb_url": f"/api/files/{row.id}/thumb?v={row.size}",
        "mime": row.mime,
        "size": row.size,
    }


async def _read_image_part(upload: UploadFile, *, label: str, max_bytes: int) -> tuple[str, bytes, str]:
    """校验并读取一个图片分片（参考图 / 遮罩共用）。"""
    mime = (upload.content_type or "").lower()
    if not mime.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"仅支持图片文件作为{label}")
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"{label}为空文件")
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"单张图片不能超过 {settings.max_upload_mb}MB")
    return (upload.filename or f"{label}.png", content, mime)


async def _run_generation(
    *,
    request: Request,
    db: AsyncSession,
    user: User,
    endpoint: str,
    json_body: dict | None = None,
    files: list | None = None,
    form: dict | None = None,
    n: int,
    model: str,
    prompt: str,
) -> list:
    base_url, api_key = await require_feature(db, "image")
    await check_content(
        db, prompt, user_id=user.id, username=user.username, ip=client_ip(request), endpoint=endpoint,
    )
    ok, take_temp, take_perm = await consume(db, user.id, "image", n)
    if not ok:
        raise HTTPException(status_code=403, detail=f"绘图次数额度不足（需要 {n} 次），请联系管理员")

    started = time.perf_counter()
    status, error, error_status = "success", "", 502
    saved = []
    try:
        async with inflight.acquire(f"user:{user.id}", await inflight_limit(db, user)):
            data = await request_json(
                "POST", base_url, api_key, endpoint, json_body=json_body, files=files, data=form
            )
        saved = await persist_image_results(
            db, user=user, base_url=base_url, api_key=api_key, data=data, prompt=prompt
        )
        if not saved:
            status, error = "failed", "上游未返回有效图片"
    except HTTPException:
        await refund(db, user.id, "image", n, take_temp, take_perm)
        raise
    except UpstreamError as exc:
        status, error = "failed", exc.message
    except StorageFullError as exc:
        status, error, error_status = "failed", str(exc), 403
    latency = int((time.perf_counter() - started) * 1000)

    failed_units = n - len(saved)
    if failed_units > 0:
        # 按成功比例退回（优先退限时组，与扣减顺序一致）
        ft = min(take_temp, failed_units)
        await refund(db, user.id, "image", failed_units, ft, failed_units - ft)
    await log_usage(
        db,
        user_id=user.id,
        feature="image",
        endpoint=endpoint,
        model=model,
        status=status,
        error=error,
        latency_ms=latency,
        units=max(len(saved), 1),
        ip=client_ip(request),
    )
    if status != "success":
        raise HTTPException(status_code=error_status, detail=error)
    return saved


@router.post("/generations", dependencies=[Depends(user_rate_limit("image"))])
async def generate(
    body: ImageGenIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    saved = await _run_generation(
        request=request,
        db=db,
        user=user,
        endpoint="/v1/images/generations",
        json_body={
            "prompt": body.prompt,
            "model": body.model,
            "n": body.n,
            "size": body.size,
            "quality": body.quality,
            "response_format": "b64_json",
        },
        n=body.n,
        model=body.model,
        prompt=body.prompt,
    )
    return {"files": [_file_out(row) for row in saved]}


@router.post("/edits", dependencies=[Depends(user_rate_limit("image"))])
async def edit(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    prompt = str(form.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="请填写提示词")
    model = str(form.get("model") or "gpt-image-2")[:100]
    size = str(form.get("size") or "")[:32] or None
    quality = str(form.get("quality") or "auto")[:16]
    try:
        n = int(form.get("n") or 1)
    except ValueError:
        n = 1
    n = min(max(n, 1), 4)

    uploads = collect_uploads(form, IMAGE_REFERENCE_FIELDS)
    if not uploads:
        raise HTTPException(status_code=400, detail="请至少上传一张参考图")
    if len(uploads) > MAX_REFERENCE_IMAGES:
        raise HTTPException(status_code=400, detail=f"参考图最多 {MAX_REFERENCE_IMAGES} 张")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    files = []
    for up in uploads:
        files.append(("image", await _read_image_part(up, label="参考图", max_bytes=max_bytes)))

    # 局部编辑：可选遮罩（不透明区域 = 需要重绘的范围），按上游 mask 字段原样透传。
    # 上游 /v1/images/edits 的字段白名单为 {image, image[], images, images[], image_url,
    # image_url[]} + {mask, mask[]}，这里用最简的 image / mask。
    masks = collect_uploads(form, MASK_REFERENCE_FIELDS)
    if len(masks) > 1:
        raise HTTPException(status_code=400, detail="遮罩只能上传一张")
    if masks:
        files.append((MASK_FIELD, await _read_image_part(masks[0], label="遮罩", max_bytes=max_bytes)))

    saved = await _run_generation(
        request=request,
        db=db,
        user=user,
        endpoint="/v1/images/edits",
        form={"prompt": prompt, "model": model, "n": str(n), "size": size or "", "quality": quality,
              "response_format": "b64_json"},
        files=files,
        n=n,
        model=model,
        prompt=prompt,
    )
    return {"files": [_file_out(row) for row in saved]}
