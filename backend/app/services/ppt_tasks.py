"""PPT/PSD 任务生命周期：提交上游、轮询同步、产物落盘。"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PptTask, StoredFile
from .storage import StorageFullError, ensure_storage_capacity, save_bytes
from .upstream import UpstreamError, download_upstream_file, request_json

ACTIVE_STATUSES = {"queued", "running"}

# 同一任务的同步操作串行化，防并发轮询重复下载/重复退款
_sync_locks: dict[str, asyncio.Lock] = {}
_sync_locks_guard = asyncio.Lock()


async def sync_lock(task_id: str) -> asyncio.Lock:
    async with _sync_locks_guard:
        lock = _sync_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            _sync_locks[task_id] = lock
        if len(_sync_locks) > 10000:
            # 只淘汰未被持有的锁，防止丢弃在途锁导致并发双同步
            for key, candidate in list(_sync_locks.items()):
                if not candidate.locked() and key != task_id:
                    _sync_locks.pop(key, None)
        return lock


async def claim_refund(session: AsyncSession, task_id: str) -> bool:
    """原子占位退款权：仅第一个把 refunded 置为 true 的调用返回 True。"""
    stmt = (
        update(PptTask)
        .where(PptTask.id == task_id)
        .where(PptTask.refunded.is_(False))
        .values(refunded=True)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount == 1


async def submit_task(
    session: AsyncSession,
    *,
    user_id: int,
    prompt: str,
    kind: str,
    base64_images: list[str],
    base_url: str,
    api_key: str,
    api_key_id: int | None = None,
) -> PptTask:
    data = await request_json(
        "POST",
        base_url,
        api_key,
        f"/v1/{kind}/generations",
        json_body={"prompt": prompt, "kind": kind, "base64_images": base64_images},
    )
    upstream_id = str(data.get("id") or "")
    if not upstream_id:
        raise UpstreamError("上游未返回任务 id")
    task = PptTask(
        id=uuid.uuid4().hex,
        upstream_task_id=upstream_id,
        user_id=user_id,
        api_key_id=api_key_id,
        kind=kind,
        prompt=prompt[:4000],
        status=str(data.get("status") or "queued"),
    )
    session.add(task)
    await session.commit()
    return task


def _pick_result_url(result: dict) -> str:
    if not isinstance(result, dict):
        return ""
    return str(result.get("primary_url") or result.get("zip_url") or "")


async def sync_task(session: AsyncSession, task: PptTask, base_url: str, api_key: str, user) -> PptTask:
    """从上游同步任务状态；成功时下载产物到本地（受用户存储配额约束）。"""
    if task.status not in ACTIVE_STATUSES:
        return task
    data = await request_json(
        "GET", base_url, api_key, "/v1/editable-file-tasks", params={"ids": task.upstream_task_id}
    )
    items = data.get("items") if isinstance(data, dict) else None
    item = items[0] if isinstance(items, list) and items else None
    if not item:
        task.status = "error"
        task.error = "上游任务不存在或已被清理"
        await session.commit()
        return task

    status = str(item.get("status") or "error")
    if status in ACTIVE_STATUSES:
        task.status = status
        await session.commit()
        return task
    if status != "success":
        task.status = "error"
        task.error = str(item.get("error") or "上游任务失败")[:500]
        await session.commit()
        return task

    url = _pick_result_url(item.get("result") or {})
    if not url:
        task.status = "error"
        task.error = "上游任务完成但未返回文件"
        await session.commit()
        return task

    try:
        content, mime = await download_upstream_file(base_url, api_key, url)
        await ensure_storage_capacity(session, user, len(content))
        ext_name = url.rsplit("/", 1)[-1] or f"{task.kind}.bin"
        rel, final_name = save_bytes(task.user_id, content, filename=ext_name, mime=mime, kind=task.kind)
    except StorageFullError as exc:
        task.status = "error"
        task.error = str(exc)[:500]
        await session.commit()
        return task
    except Exception as exc:
        # 磁盘错误等兜底：置为失败态，让退款链路接管，避免任务永久卡死
        task.status = "error"
        task.error = f"产物保存失败：{exc.__class__.__name__}"[:500]
        await session.commit()
        return task
    row = StoredFile(
        user_id=task.user_id,
        kind=task.kind,
        filename=final_name,
        path=rel,
        mime=mime,
        size=len(content),
        prompt=task.prompt[:2000],
    )
    session.add(row)
    await session.flush()
    task.file_id = row.id
    task.status = "success"
    await session.commit()
    return task
