"""管理端：存储统计与手动清理。"""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as app_settings
from ..database import engine, get_db
from ..deps import require_admin
from ..models import AuditLog, RiskEvent, StoredFile, UsageLog, User, UserSession
from ..services.cleanup import _int_or, count_orphan_files, is_running, run_cleanup
from ..services.settings_store import get_all_settings
from ..services.storage import data_root, dir_usage
from ..services.thumbnails import thumbs_root
from ..services.usage import audit

router = APIRouter(prefix="/api/admin/storage", tags=["admin-storage"], dependencies=[Depends(require_admin)])


def _db_file_bytes() -> int | None:
    """SQLite 模式返回库文件（含 WAL/SHM）体积；其他数据库返回 None。"""
    url = app_settings.database_url
    if not url.startswith("sqlite"):
        return None
    path = Path(url.split("///", 1)[-1])
    total = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(path) + suffix)
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


async def _pg_db_bytes(db: AsyncSession) -> int | None:
    try:
        return int(await db.scalar(text("SELECT pg_database_size(current_database())")) or 0)
    except Exception:
        return None


@router.get("")
async def storage_overview(db: AsyncSession = Depends(get_db)):
    total_bytes = await db.scalar(select(func.coalesce(func.sum(StoredFile.size), 0))) or 0
    total_files = await db.scalar(select(func.count(StoredFile.id))) or 0
    total_logs = await db.scalar(select(func.count(UsageLog.id))) or 0

    # 产物按类型拆分（图片 / PPT / PSD）
    kind_rows = await db.execute(
        select(StoredFile.kind, func.coalesce(func.sum(StoredFile.size), 0), func.count(StoredFile.id))
        .group_by(StoredFile.kind)
    )
    by_kind = [
        {"kind": k, "bytes": int(b), "files": int(c)} for k, b, c in kind_rows.all()
    ]

    top_rows = await db.execute(
        select(User.username, func.coalesce(func.sum(StoredFile.size), 0).label("bytes"),
               func.count(StoredFile.id).label("files"))
        .join(StoredFile, StoredFile.user_id == User.id)
        .group_by(User.username)
        .order_by(func.sum(StoredFile.size).desc())
        .limit(10)
    )

    settings = await get_all_settings(db)
    retention_hours = _int_or(settings.get("file_retention_hours"), 0)
    log_hours = _int_or(settings.get("log_retention_hours"), 168)
    audit_days = _int_or(settings.get("audit_retention_days"), 90)

    # 磁盘整体占用（数据目录所在分区）
    disk = shutil.disk_usage(data_root())

    # 数据库体积
    dialect = engine.dialect.name
    if dialect == "sqlite":
        db_bytes = _db_file_bytes()
    elif dialect == "postgresql":
        db_bytes = await _pg_db_bytes(db)
    else:
        db_bytes = None

    # 可清理预估（与自动清理同一套规则，只读统计）
    now = datetime.now(timezone.utc)
    cleanable: dict = {}
    if retention_hours > 0:
        cutoff = now - timedelta(hours=retention_hours)
        cleanable["expired_files"] = await db.scalar(
            select(func.count(StoredFile.id)).where(StoredFile.created_at < cutoff)
        ) or 0
        cleanable["expired_bytes"] = await db.scalar(
            select(func.coalesce(func.sum(StoredFile.size), 0)).where(StoredFile.created_at < cutoff)
        ) or 0
    else:
        cleanable["expired_files"] = 0
        cleanable["expired_bytes"] = 0

    orphan_count, orphan_bytes = await count_orphan_files(db)
    cleanable["orphan_files"] = orphan_count
    cleanable["orphan_bytes"] = orphan_bytes

    if log_hours > 0:
        cutoff = now - timedelta(hours=log_hours)
        cleanable["old_usage_logs"] = await db.scalar(
            select(func.count(UsageLog.id)).where(UsageLog.created_at < cutoff)
        ) or 0
        cleanable["old_risk_events"] = await db.scalar(
            select(func.count(RiskEvent.id)).where(RiskEvent.created_at < cutoff)
        ) or 0
    else:
        cleanable["old_usage_logs"] = 0
        cleanable["old_risk_events"] = 0
    if audit_days > 0:
        cutoff = now - timedelta(days=audit_days)
        cleanable["old_audit_logs"] = await db.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.created_at < cutoff)
        ) or 0
    else:
        cleanable["old_audit_logs"] = 0

    session_cutoff = now - timedelta(days=30)
    cleanable["stale_sessions"] = await db.scalar(
        select(func.count(UserSession.id)).where(
            (UserSession.revoked.is_(True)) | (UserSession.last_seen_at < session_cutoff)
        )
    ) or 0

    thumb_bytes, thumb_files = dir_usage(thumbs_root())

    # 最近一次手动清理（审计日志）
    last_cleanup = (
        await db.execute(
            select(AuditLog.created_at, AuditLog.detail)
            .where(AuditLog.action == "storage.cleanup")
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).first()

    return {
        "total_bytes": total_bytes,
        "total_files": total_files,
        "total_logs": total_logs,
        "by_kind": by_kind,
        "cleanup_running": is_running(),
        "top_users": [{"username": u, "bytes": b, "files": f} for u, b, f in top_rows.all()],
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "db_bytes": db_bytes,
        "cleanable": cleanable,
        "last_cleanup": {
            "at": last_cleanup[0].isoformat() if last_cleanup else None,
            "detail": last_cleanup[1] if last_cleanup else "",
        },
        # 缩略图单独统计：它们不进用户配额（不占 StoredFile.size），但确实占磁盘
        "thumbnail_bytes": thumb_bytes,
        "thumbnail_files": thumb_files,
        "storage_quota_mb_default": _int_or(settings.get("storage_quota_mb_default"), 0),
        "file_retention_hours": retention_hours,
        "log_retention_hours": log_hours,
        "audit_retention_days": audit_days,
    }


@router.post("/cleanup")
async def cleanup_now(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """后台异步执行一轮清理（库大时可能较久），立即返回。"""
    if is_running():
        raise HTTPException(status_code=409, detail="已有清理任务在运行，请稍后再试")

    async def _run() -> None:
        stats = await run_cleanup()
        from ..database import SessionLocal
        async with SessionLocal() as session:
            await audit(session, admin_id=admin.id, action="storage.cleanup", detail=str(stats))

    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"ok": True, "started": True}


# 持有后台任务引用，防止被 GC 提前回收
_bg_tasks: set[asyncio.Task] = set()
