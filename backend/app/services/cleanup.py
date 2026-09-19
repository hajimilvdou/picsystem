"""自动清理：过期产物、旧日志、孤儿文件。

后台任务每小时执行一轮；管理员也可手动触发（后台异步执行）。
互斥锁保证任何时刻只有一轮清理在运行。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import SessionLocal
from ..models import AuditLog, RiskEvent, StoredFile, UsageLog, UserSession
from .settings_store import get_setting
from .storage import data_root, delete_file
from .thumbnails import thumb_abs_path, thumbs_root

logger = logging.getLogger("picsystem.cleanup")

# 孤儿判定的宽限期：跳过最近写入的文件，避免误删"磁盘已写、DB 未提交"的在途产物
_ORPHAN_GRACE_SECONDS = 2 * 3600
# 单轮日志删除总量上限，超出部分留待下轮
_LOG_DELETE_BUDGET = 50000
# 日志分批大小，避免 SQLite 长事务写锁 / PostgreSQL 长事务
_LOG_BATCH = 5000

_run_lock = asyncio.Lock()
_running = False


def is_running() -> bool:
    return _running


async def cleanup_files(session: AsyncSession, retention_hours: int) -> int:
    """删除超过保留时长的产物（DB 行 + 磁盘文件），返回删除数。"""
    if retention_hours <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    rows = (
        await session.execute(select(StoredFile).where(StoredFile.created_at < cutoff).limit(500))
    ).scalars().all()
    ids = [row.id for row in rows]
    for row in rows:
        delete_file(row.path)
    if ids:
        # core 批量删除，容忍并发下已消失的行
        await session.execute(delete(StoredFile).where(StoredFile.id.in_(ids)))
        await session.commit()
    return len(ids)


async def _batched_delete(session: AsyncSession, model, cutoff: datetime, budget: int) -> int:
    """分批删除过期行，批间提交，返回删除总数。"""
    removed = 0
    while removed < budget:
        ids = (
            await session.execute(
                select(model.id).where(model.created_at < cutoff).limit(min(_LOG_BATCH, budget - removed))
            )
        ).scalars().all()
        if not ids:
            break
        await session.execute(delete(model).where(model.id.in_(ids)))
        await session.commit()
        removed += len(ids)
        if len(ids) < _LOG_BATCH:
            break
    return removed


async def cleanup_logs(session: AsyncSession, log_retention_hours: int, audit_retention_days: int) -> dict[str, int]:
    """按保留策略清理调用日志 / 风控事件 / 审计日志（分批，单轮限量）。"""
    removed = {"usage_logs": 0, "risk_events": 0, "audit_logs": 0}
    if log_retention_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=log_retention_hours)
        removed["usage_logs"] = await _batched_delete(session, UsageLog, cutoff, _LOG_DELETE_BUDGET)
        removed["risk_events"] = await _batched_delete(session, RiskEvent, cutoff, _LOG_DELETE_BUDGET)
    if audit_retention_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=audit_retention_days)
        removed["audit_logs"] = await _batched_delete(session, AuditLog, cutoff, _LOG_DELETE_BUDGET)
    return removed


async def cleanup_orphan_files(session: AsyncSession) -> int:
    """清理磁盘上存在但数据库无记录的孤儿文件（如同步中断残留）。

    跳过 mtime 距今小于宽限期的文件，防止误删在途写入；每轮设置扫描上限。
    """
    files_root = data_root() / "files"
    if not files_root.exists():
        return 0
    orphans = await _find_orphans(session, files_root)
    removed = 0
    for path in orphans:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    # 清理空目录（单遍收集后按深度倒序删除）
    dirs = sorted((p for p in files_root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts))
    for d in dirs:
        try:
            d.rmdir()
        except OSError:
            pass
    return removed


async def _find_orphans(session: AsyncSession, files_root) -> list:
    """找出磁盘孤儿文件（带宽限期与扫描上限），只读不删。"""
    rows = await session.execute(select(StoredFile.path))
    known = {p for (p,) in rows.all()}
    orphans: list = []
    scanned = 0
    grace_before = time.time() - _ORPHAN_GRACE_SECONDS
    for path in files_root.rglob("*"):
        if not path.is_file():
            continue
        scanned += 1
        if scanned > 20000:  # 单轮扫描上限，防极端情况拖垮任务
            break
        try:
            if path.stat().st_mtime > grace_before:
                continue  # 新文件，可能在途
        except OSError:
            continue
        rel = path.relative_to(data_root()).as_posix()
        if rel not in known:
            orphans.append(path)
    return orphans


async def cleanup_orphan_thumbs(session: AsyncSession) -> int:
    """清理 `thumbs/` 下没有对应产物的残留缩略图。

    正常路径下删除产物时会连带删缩略图（见 storage.delete_file），这里是崩溃/异常后的兜底。
    做法：以数据库中的产物路径推导出「应有的缩略图集合」，删掉集合外的文件；
    顺带清掉生成中断留下的 `.tmp`。只记日志，不影响 `cleanup_orphan_files` 的既有返回值。
    """
    root = thumbs_root()
    if not root.exists():
        return 0
    rows = await session.execute(select(StoredFile.path))
    expected = {thumb_abs_path(rel) for (rel,) in rows.all() if rel}
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        stale_tmp = path.suffix == ".tmp"
        if not stale_tmp and path in expected:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


async def count_orphan_files(session: AsyncSession) -> tuple[int, int]:
    """只读统计孤儿文件数量与体积，供管理端预览可清理空间。"""
    files_root = data_root() / "files"
    if not files_root.exists():
        return 0, 0
    orphans = await _find_orphans(session, files_root)
    total = 0
    for p in orphans:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return len(orphans), total


def _int_or(value, default: int) -> int:
    """设置值容错转换，防人工改库产生脏数据导致清理崩溃。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def cleanup_sessions(session: AsyncSession) -> int:
    """删除已吊销或超过 30 天未活跃的设备会话记录。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    result = await session.execute(
        delete(UserSession).where((UserSession.revoked.is_(True)) | (UserSession.last_seen_at < cutoff))
    )
    await session.commit()
    return result.rowcount or 0


async def run_cleanup() -> dict:
    """执行一轮清理，返回统计。互斥运行，重入直接返回。"""
    global _running
    if _run_lock.locked():
        return {"skipped": True, "reason": "已有清理任务在运行"}
    async with _run_lock:
        _running = True
        try:
            async with SessionLocal() as session:
                retention_hours = _int_or(await get_setting(session, "file_retention_hours"), 0)
                log_hours = _int_or(await get_setting(session, "log_retention_hours"), 168)
                audit_days = _int_or(await get_setting(session, "audit_retention_days"), 90)

                expired = await cleanup_files(session, retention_hours)
                logs = await cleanup_logs(session, log_hours, audit_days)
                orphans = await cleanup_orphan_files(session)
                orphan_thumbs = await cleanup_orphan_thumbs(session)
                sessions = await cleanup_sessions(session)
                stats = {"expired_files": expired, "orphan_files": orphans,
                         "orphan_thumbs": orphan_thumbs, "sessions": sessions, **logs}
                if any(stats.values()):
                    logger.info("自动清理完成：%s", stats)
                return stats
        finally:
            _running = False


async def cleanup_loop(interval_seconds: int = 3600) -> None:
    """后台循环清理任务：启动后先静默执行一轮，之后每小时一轮。"""
    while True:
        try:
            await run_cleanup()
        except Exception:
            logger.exception("自动清理任务执行失败")
        await asyncio.sleep(interval_seconds)
