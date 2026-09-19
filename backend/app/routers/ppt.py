"""PPT / PSD 任务路由。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import client_ip, get_current_user, inflight, inflight_limit, user_rate_limit
from ..models import PptTask, User
from ..schemas import PptGenIn
from ..services.content_guard import check_content
from ..services.guard import require_feature
from ..services.ppt_tasks import claim_refund, submit_task, sync_lock, sync_task
from ..services.quota import consume, refund
from ..services.upstream import UpstreamError
from ..services.usage import log_usage

router = APIRouter(prefix="/api/ppt", tags=["ppt"])


def _task_out(task: PptTask) -> dict:
    return {
        "id": task.id,
        "kind": task.kind,
        "prompt": task.prompt[:200],
        "status": task.status,
        "error": task.error,
        "file_id": task.file_id,
        "download_url": f"/api/files/{task.file_id}/download" if task.file_id else None,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


@router.post("/generations", dependencies=[Depends(user_rate_limit("ppt"))])
async def create_task(
    body: PptGenIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base_url, api_key = await require_feature(db, "ppt")
    await check_content(
        db, body.prompt, user_id=user.id, username=user.username,
        ip=client_ip(request), endpoint=f"/v1/{body.kind}/generations",
    )
    ok, take_temp, take_perm = await consume(db, user.id, "ppt", 1)
    if not ok:
        raise HTTPException(status_code=403, detail="PPT/PSD 次数额度不足，请联系管理员")

    started = time.perf_counter()
    status, error = "success", ""
    task: PptTask | None = None
    try:
        async with inflight.acquire(f"user:{user.id}", await inflight_limit(db, user)):
            task = await submit_task(
                db,
                user_id=user.id,
                prompt=body.prompt,
                kind=body.kind,
                base64_images=body.base64_images,
                base_url=base_url,
                api_key=api_key,
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
        db,
        user_id=user.id,
        feature="ppt",
        endpoint=f"/v1/{body.kind}/generations",
        model=body.kind,
        status=status,
        error=error,
        latency_ms=latency,
        ip=client_ip(request),
    )
    if status != "success":
        raise HTTPException(status_code=502, detail=error)
    return _task_out(task)


@router.get("/tasks")
async def list_tasks(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PptTask).where(PptTask.user_id == user.id).order_by(PptTask.created_at.desc()).limit(100)
    )
    return {"items": [_task_out(t) for t in result.scalars()]}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(PptTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status in ("queued", "running"):
        base_url, api_key = await require_feature(db, "ppt")
        # 同一任务串行同步；只有真正发生状态迁移的请求才负责记账
        transitioned = False
        lock = await sync_lock(task.id)
        async with lock:
            await db.refresh(task)
            if task.status in ("queued", "running"):
                try:
                    task = await sync_task(db, task, base_url, api_key, user=user)
                    transitioned = task.status in ("success", "error")
                except UpstreamError as exc:
                    raise HTTPException(status_code=502, detail=exc.message) from exc
        if transitioned and task.status == "error" and await claim_refund(db, task.id):
            await refund(db, user.id, "ppt", 1)
            await log_usage(
                db,
                user_id=user.id,
                feature="ppt",
                endpoint=f"/v1/{task.kind}/generations",
                model=task.kind,
                status="failed",
                error=task.error,
                ip=client_ip(request),
            )
        elif transitioned and task.status == "success":
            await log_usage(
                db,
                user_id=user.id,
                feature="ppt",
                endpoint=f"/v1/{task.kind}/generations",
                model=task.kind,
                status="success",
                ip=client_ip(request),
            )
    return _task_out(task)
