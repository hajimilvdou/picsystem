"""OpenAI 兼容的用户密钥 API（newapi 风格）。

- 鉴权：Authorization: Bearer sk-...
- 每个密钥归属一个用户，按用户额度按次扣费，失败退还
- 生成的图片/文件落本地存储，url 形式响应指向本站 /v1/files/{id}/download
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import SessionLocal, get_db
from ..deps import client_ip, inflight, rate_limiter
from ..models import ApiKey, PptTask, StoredFile, User, UserStatus
from ..security import hash_api_key
from ..services.content_guard import check_content, extract_texts
from ..services.guard import require_feature
from ..services.image_results import persist_image_results
from ..services.ppt_tasks import ACTIVE_STATUSES, claim_refund, submit_task, sync_lock, sync_task
from ..services.quota import consume, refund
from ..services.risk import record_event
from ..services.settings_store import feature_flags
from ..services.storage import StorageFullError, resolve_path
from ..services.upstream import UpstreamError, get_client, request_json, upstream_config
from ..services.usage import log_usage

router = APIRouter(prefix="/v1", tags=["v1"])


def _openai_error(message: str, status: int, err_type: str = "invalid_request_error") -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"message": message, "type": err_type}})


async def get_key_identity(request: Request, db: AsyncSession = Depends(get_db)) -> tuple[User, ApiKey]:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise _openai_error("缺少 Bearer 密钥", 401, "authentication_error")
    raw = auth[7:].strip()
    if not raw.startswith("sk-"):
        raise _openai_error("密钥格式无效", 401, "authentication_error")
    row = (
        await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw)))
    ).scalar_one_or_none()
    if row is None or not row.enabled:
        raise _openai_error("密钥无效或已停用", 401, "authentication_error")
    user = await db.get(User, row.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise _openai_error("账号不可用", 401, "authentication_error")
    # 单密钥限流：60 次/分钟
    if not await rate_limiter.hit(f"v1:{row.id}", 60, 60):
        await record_event(
            db, kind="rate_limit", username=user.username, user_id=user.id,
            ip=client_ip(request), detail="API 密钥超过每分钟 60 次",
        )
        raise _openai_error("请求过于频繁", 429, "rate_limit_error")
    row.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    return user, row


async def _check_feature(db: AsyncSession, feature: str):
    flags = await feature_flags(db)
    if not flags.get(feature, False):
        raise _openai_error(f"功能 {feature} 已被管理员关闭", 403, "permission_error")


@router.get("/models")
async def models(identity: tuple[User, ApiKey] = Depends(get_key_identity), db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    try:
        return await request_json("GET", base_url, api_key, "/v1/models")
    except UpstreamError as exc:
        raise _openai_error(exc.message, 502, "upstream_error") from exc


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    identity: tuple[User, ApiKey] = Depends(get_key_identity),
    db: AsyncSession = Depends(get_db),
):
    user, key_row = identity
    await _check_feature(db, "chat")
    try:
        body = await request.json()
    except Exception:
        raise _openai_error("请求体必须是 JSON", 400) from None
    model = str(body.get("model") or "auto")
    if not body.get("messages"):
        raise _openai_error("messages 不能为空", 400)
    # 全量文本检查（不截断），防后置/拆词绕过
    for text in extract_texts(body.get("messages")):
        await check_content(
            db, text, user_id=user.id, username=user.username, ip=client_ip(request),
            endpoint="/v1/chat/completions",
        )

    ok, take_temp, take_perm = await consume(db, user.id, "chat", 1)
    if not ok:
        raise _openai_error("对话额度不足", 429, "insufficient_quota")
    base_url, upstream_key = await upstream_config(db)
    want_stream = bool(body.get("stream"))
    started = time.perf_counter()
    ip = client_ip(request)

    # 先抢并发槽：槽满时直接 429（退款），不进入流式响应
    cm = inflight.acquire(f"key:{key_row.id}", settings.max_inflight_per_key)
    try:
        await cm.__aenter__()
    except HTTPException:
        await refund(db, user.id, "chat", 1, take_temp, take_perm)
        raise

    if want_stream:
        user_id, key_id = user.id, key_row.id

        async def passthrough():
            status, error = "success", ""
            try:
                client = get_client()
                async with client.stream(
                    "POST",
                    f"{base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {upstream_key}"},
                    json=body,
                ) as resp:
                    if resp.status_code >= 400:
                        content = await resp.aread()
                        status, error = "failed", f"upstream HTTP {resp.status_code}"
                        yield content
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            except Exception as exc:  # 连接中断等
                status, error = "failed", exc.__class__.__name__
            finally:
                await cm.__aexit__(None, None, None)
                latency = int((time.perf_counter() - started) * 1000)
                # 流式响应期间请求级 db 可能已被回收，使用独立会话
                async with SessionLocal() as session:
                    if status != "success":
                        await refund(session, user_id, "chat", 1, take_temp, take_perm)
                    await log_usage(
                        session, user_id=user_id, api_key_id=key_id, feature="chat",
                        endpoint="/v1/chat/completions", model=model, status=status,
                        error=error, latency_ms=latency, ip=ip,
                    )

        return StreamingResponse(
            passthrough(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    status, error = "success", ""
    usage: dict = {}
    try:
        data = await request_json("POST", base_url, upstream_key, "/v1/chat/completions", json_body=body)
        usage = data.get("usage") or {}
        result = data
    except UpstreamError as exc:
        status, error = "failed", exc.message
        result = None
    finally:
        await cm.__aexit__(None, None, None)
    latency = int((time.perf_counter() - started) * 1000)
    if status != "success":
        await refund(db, user.id, "chat", 1, take_temp, take_perm)
    await log_usage(
        db, user_id=user.id, api_key_id=key_row.id, feature="chat",
        endpoint="/v1/chat/completions", model=model, status=status, error=error,
        latency_ms=latency, ip=ip,
        prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
    )
    if result is None:
        raise _openai_error(error, 502, "upstream_error")
    return result


@router.post("/search")
async def search(
    request: Request,
    identity: tuple[User, ApiKey] = Depends(get_key_identity),
    db: AsyncSession = Depends(get_db),
):
    user, key_row = identity
    await _check_feature(db, "search")
    try:
        body = await request.json()
    except Exception:
        raise _openai_error("请求体必须是 JSON", 400) from None
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise _openai_error("prompt 不能为空", 400)
    await check_content(
        db, prompt, user_id=user.id, username=user.username, ip=client_ip(request), endpoint="/v1/search",
    )
    ok, take_temp, take_perm = await consume(db, user.id, "search", 1)
    if not ok:
        raise _openai_error("搜索额度不足", 429, "insufficient_quota")
    base_url, upstream_key = await upstream_config(db)
    started = time.perf_counter()
    status, error, result = "success", "", None
    try:
        async with inflight.acquire(f"key:{key_row.id}", settings.max_inflight_per_key):
            result = await request_json("POST", base_url, upstream_key, "/v1/search", json_body={"prompt": prompt})
    except HTTPException:
        await refund(db, user.id, "search", 1, take_temp, take_perm)
        raise
    except UpstreamError as exc:
        status, error = "failed", exc.message
    latency = int((time.perf_counter() - started) * 1000)
    if status != "success":
        await refund(db, user.id, "search", 1, take_temp, take_perm)
    await log_usage(
        db, user_id=user.id, api_key_id=key_row.id, feature="search", endpoint="/v1/search",
        model="web-search", status=status, error=error, latency_ms=latency, ip=client_ip(request),
    )
    if result is None:
        raise _openai_error(error, 502, "upstream_error")
    return result


def _public_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _image_request(
    request: Request,
    db: AsyncSession,
    identity: tuple[User, ApiKey],
    *,
    endpoint: str,
    n: int,
    model: str,
    json_body: dict | None = None,
    form: dict | None = None,
    files: list | None = None,
    response_format: str = "b64_json",
):
    user, key_row = identity
    await _check_feature(db, "image")
    prompt_text = str((json_body or form or {}).get("prompt") or "")
    if prompt_text:
        await check_content(
            db, prompt_text, user_id=user.id, username=user.username,
            ip=client_ip(request), endpoint=endpoint,
        )
    try:
        n = min(max(int(n or 1), 1), 4)
    except (TypeError, ValueError):
        raise _openai_error("n 必须是 1-4 的整数", 400) from None
    ok, take_temp, take_perm = await consume(db, user.id, "image", n)
    if not ok:
        raise _openai_error(f"绘图额度不足（需要 {n} 次）", 429, "insufficient_quota")
    base_url, upstream_key = await upstream_config(db)
    started = time.perf_counter()
    status, error, openai_status = "success", "", 502
    saved = []
    try:
        async with inflight.acquire(f"key:{key_row.id}", settings.max_inflight_per_key):
            data = await request_json(
                "POST", base_url, upstream_key, endpoint, json_body=json_body, data=form, files=files
            )
        saved = await persist_image_results(
            db, user=user, base_url=base_url, api_key=upstream_key, data=data,
            prompt=str((json_body or form or {}).get("prompt") or ""),
        )
        if not saved:
            status, error = "failed", "上游未返回有效图片"
    except HTTPException:
        await refund(db, user.id, "image", n, take_temp, take_perm)
        raise
    except UpstreamError as exc:
        status, error = "failed", exc.message
    except StorageFullError as exc:
        status, error, openai_status = "failed", str(exc), 403
    latency = int((time.perf_counter() - started) * 1000)
    failed_units = n - len(saved)
    if failed_units > 0:
        ft = min(take_temp, failed_units)
        await refund(db, user.id, "image", failed_units, ft, failed_units - ft)
    await log_usage(
        db, user_id=user.id, api_key_id=key_row.id, feature="image", endpoint=endpoint,
        model=model, status=status, error=error, latency_ms=latency,
        units=max(len(saved), 1), ip=client_ip(request),
    )
    if status != "success":
        raise _openai_error(error, openai_status, "upstream_error" if openai_status == 502 else "permission_error")

    base = _public_base(request)
    out = []
    for row in saved:
        if response_format == "url":
            out.append({"url": f"{base}/v1/files/{row.id}/download"})
        else:
            path = resolve_path(row.path)
            out.append({"b64_json": base64.b64encode(path.read_bytes()).decode()})
    return {"created": int(time.time()), "data": out}


@router.post("/images/generations")
async def image_generations(
    request: Request,
    identity: tuple[User, ApiKey] = Depends(get_key_identity),
    db: AsyncSession = Depends(get_db),
):
    try:
        body = await request.json()
    except Exception:
        raise _openai_error("请求体必须是 JSON", 400) from None
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise _openai_error("prompt 不能为空", 400)
    model = str(body.get("model") or "gpt-image-2")
    response_format = str(body.get("response_format") or "b64_json")
    try:
        n = int(body.get("n") or 1)
    except (TypeError, ValueError):
        raise _openai_error("n 必须是 1-4 的整数", 400) from None
    return await _image_request(
        request, db, identity,
        endpoint="/v1/images/generations",
        n=n,
        model=model,
        json_body={
            "prompt": prompt,
            "model": model,
            "n": min(max(n, 1), 4),
            "size": body.get("size"),
            "quality": str(body.get("quality") or "auto"),
            "response_format": "b64_json",
        },
        response_format=response_format,
    )


@router.post("/images/edits")
async def image_edits(
    request: Request,
    identity: tuple[User, ApiKey] = Depends(get_key_identity),
    db: AsyncSession = Depends(get_db),
):
    form_data = await request.form()
    prompt = str(form_data.get("prompt") or "").strip()
    if not prompt:
        raise _openai_error("prompt 不能为空", 400)
    model = str(form_data.get("model") or "gpt-image-2")
    response_format = str(form_data.get("response_format") or "b64_json")
    uploads = [v for v in form_data.getlist("image") if hasattr(v, "read")]
    if not uploads:
        raise _openai_error("缺少 image 文件", 400)
    if len(uploads) > 4:
        raise _openai_error("参考图最多 4 张", 400)
    files = []
    max_bytes = settings.max_upload_mb * 1024 * 1024
    for up in uploads:
        content = await up.read()
        if len(content) > max_bytes:
            raise _openai_error(f"单张图片不能超过 {settings.max_upload_mb}MB", 400)
        files.append(("image", (up.filename or "image.png", content, up.content_type or "image/png")))
    try:
        n = int(form_data.get("n") or 1)
    except ValueError:
        n = 1
    return await _image_request(
        request, db, identity,
        endpoint="/v1/images/edits",
        n=n,
        model=model,
        form={
            "prompt": prompt, "model": model, "n": str(min(max(n, 1), 4)),
            "size": str(form_data.get("size") or ""), "quality": str(form_data.get("quality") or "auto"),
            "response_format": "b64_json",
        },
        files=files,
        response_format=response_format,
    )


async def _submit_ppt_kind(
    kind: str,
    request: Request,
    identity: tuple[User, ApiKey],
    db: AsyncSession,
):
    user, key_row = identity
    await _check_feature(db, "ppt")
    try:
        body = await request.json()
    except Exception:
        raise _openai_error("请求体必须是 JSON", 400) from None
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise _openai_error("prompt 不能为空", 400)
    await check_content(
        db, prompt, user_id=user.id, username=user.username, ip=client_ip(request),
        endpoint=f"/v1/{kind}/generations",
    )
    base64_images = body.get("base64_images")
    if not isinstance(base64_images, list):
        base64_images = []
    base64_images = [str(item) for item in base64_images[:4]]
    for item in base64_images:
        if len(item) > 14 * 1024 * 1024:  # base64 编码后约 10MB 原始字节
            raise _openai_error("单张参考图不能超过 10MB", 400)
    ok, take_temp, take_perm = await consume(db, user.id, "ppt", 1)
    if not ok:
        raise _openai_error("PPT/PSD 额度不足", 429, "insufficient_quota")
    base_url, upstream_key = await upstream_config(db)
    started = time.perf_counter()
    status, error, task = "success", "", None
    try:
        async with inflight.acquire(f"key:{key_row.id}", settings.max_inflight_per_key):
            task = await submit_task(
                db, user_id=user.id, prompt=prompt, kind=kind,
                base64_images=base64_images, base_url=base_url,
                api_key=upstream_key, api_key_id=key_row.id,
            )
    except HTTPException:
        await refund(db, user.id, "ppt", 1, take_temp, take_perm)
        raise
    except UpstreamError as exc:
        status, error = "failed", exc.message
    latency = int((time.perf_counter() - started) * 1000)
    if status != "success":
        await refund(db, user.id, "ppt", 1, take_temp, take_perm)
    await log_usage(
        db, user_id=user.id, api_key_id=key_row.id, feature="ppt",
        endpoint=f"/v1/{kind}/generations", model=kind, status=status,
        error=error, latency_ms=latency, ip=client_ip(request),
    )
    if task is None:
        raise _openai_error(error, 502, "upstream_error")
    return {"id": task.id, "status": task.status, "kind": kind, "is_active": task.status in ACTIVE_STATUSES}


@router.post("/ppt/generations")
async def ppt_generations(request: Request, identity=Depends(get_key_identity), db: AsyncSession = Depends(get_db)):
    return await _submit_ppt_kind("ppt", request, identity, db)


@router.post("/psd/generations")
async def psd_generations(request: Request, identity=Depends(get_key_identity), db: AsyncSession = Depends(get_db)):
    return await _submit_ppt_kind("psd", request, identity, db)


@router.get("/editable-file-tasks")
async def editable_file_tasks(
    request: Request,
    ids: str = "",
    identity: tuple[User, ApiKey] = Depends(get_key_identity),
    db: AsyncSession = Depends(get_db),
):
    user, key_row = identity
    task_ids = [item.strip() for item in ids.split(",") if item.strip()][:50]
    if not task_ids:
        raise _openai_error("ids 不能为空", 400)
    base_url, upstream_key = await upstream_config(db)
    items = []
    missing = []
    for task_id in task_ids:
        task = await db.get(PptTask, task_id)
        if task is None or task.user_id != user.id:
            missing.append(task_id)
            continue
        if task.status in ACTIVE_STATUSES:
            lock = await sync_lock(task.id)
            transitioned = False
            async with lock:
                await db.refresh(task)
                if task.status in ACTIVE_STATUSES:
                    try:
                        task = await sync_task(db, task, base_url, upstream_key, user=user)
                        transitioned = task.status in ("success", "error")
                    except UpstreamError:
                        pass
            if transitioned and task.status == "error" and await claim_refund(db, task.id):
                await refund(db, user.id, "ppt", 1)
                await log_usage(
                    db, user_id=user.id, api_key_id=key_row.id, feature="ppt",
                    endpoint=f"/v1/{task.kind}/generations", model=task.kind,
                    status="failed", error=task.error, ip=client_ip(request),
                )
            elif transitioned and task.status == "success":
                await log_usage(
                    db, user_id=user.id, api_key_id=key_row.id, feature="ppt",
                    endpoint=f"/v1/{task.kind}/generations", model=task.kind,
                    status="success", ip=client_ip(request),
                )
        item = {
            "id": task.id,
            "status": task.status,
            "kind": task.kind,
            "is_active": task.status in ACTIVE_STATUSES,
            "error": task.error or None,
            "result": None,
        }
        if task.status == "success" and task.file_id:
            item["result"] = {
                "primary_url": f"{_public_base(request)}/v1/files/{task.file_id}/download"
            }
        items.append(item)
    return {"items": items, "missing_ids": missing}


@router.get("/files/{file_id}/download")
async def download_generated_file(
    file_id: int,
    identity: tuple[User, ApiKey] = Depends(get_key_identity),
    db: AsyncSession = Depends(get_db),
):
    user, _key = identity
    row = await db.get(StoredFile, file_id)
    if row is None or row.user_id != user.id:
        raise _openai_error("文件不存在", 404, "not_found")
    try:
        path = resolve_path(row.path)
    except PermissionError:
        raise _openai_error("文件不存在", 404, "not_found") from None
    if not path.exists():
        raise _openai_error("文件已被清理", 404, "not_found")
    return FileResponse(path, media_type=row.mime, filename=row.filename)


# OpenAI 错误格式：HTTPException detail 原样输出
@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def fallback(path: str):
    return JSONResponse(
        status_code=404,
        content={"error": {"message": f"未知接口 /v1/{path}", "type": "invalid_request_error"}},
    )
