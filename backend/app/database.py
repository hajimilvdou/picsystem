"""数据库引擎与会话。"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

log = logging.getLogger("picsystem.db")


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = settings.database_url
    kwargs: dict = {"echo": False, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # 确保 SQLite 数据目录存在
        db_path = url.split("///")[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return create_async_engine(url, **kwargs)


engine = _make_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

if settings.database_url.startswith("sqlite"):
    # SQLite 默认不强制外键；打开以保证级联删除生效（PostgreSQL 由 DDL 保证）
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_fk_on(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def get_db():
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """启动时初始化数据库：版本化迁移 → 建表 → 默认设置与初始管理员。

    顺序上先跑迁移再 create_all：
    - 历史库：迁移只负责把「已存在的表」补到最新结构（幂等，数据不丢）；
    - 全新库：迁移全部空跑（表还不存在），create_all 一次建出最新结构。

    迁移失败会抛异常中止启动，避免带着半截 schema 对外服务。
    迁移脚本与约定见 docs/MIGRATIONS.md。
    """
    from . import models  # noqa: F401  确保模型已注册
    from .migrations import run_migrations
    from .services.bootstrap import seed_defaults

    report = await run_migrations(engine)
    if report.applied:
        log.info("[迁移] %s", report.summary())
        for item in report.applied:
            log.info("[迁移] %s %s（%d 处改动，%dms）", item.id, item.description, item.changed, item.duration_ms)
    for migration_id in report.edited:
        log.warning("[迁移] %s 在应用后被修改过，其改动不会生效（请新增迁移而不是改旧的）", migration_id)
    for label in report.skipped_destructive:
        log.warning("[迁移] %s 为破坏性迁移，已按 MIGRATIONS_SKIP_DESTRUCTIVE 跳过", label)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        await seed_defaults(session)
        await session.commit()
