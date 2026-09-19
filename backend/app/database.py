"""数据库引擎与会话。"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


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
    """建表 + 轻量列迁移 + 写入默认设置 + 创建初始管理员。"""
    from . import models  # noqa: F401  确保模型已注册
    from .services.bootstrap import seed_defaults

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_lightweight_migrations)
    async with SessionLocal() as session:
        await seed_defaults(session)
        await session.commit()


def _lightweight_migrations(sync_conn) -> None:
    """create_all 不会修改已有表；对新增列做幂等 ADD COLUMN / CREATE INDEX。"""
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)

    def add_col(table: str, name: str, ddl: str) -> bool:
        cols = {c["name"] for c in inspector.get_columns(table)}
        if name not in cols:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
            return True
        return False

    if "users" in inspector.get_table_names():
        add_col("users", "storage_limit_mb", "storage_limit_mb INTEGER")
        add_col("users", "reg_fp", "reg_fp VARCHAR(128) DEFAULT '' NOT NULL")
        add_col("users", "reg_ip", "reg_ip VARCHAR(64) DEFAULT '' NOT NULL")
        add_col("users", "agreement_version", "agreement_version INTEGER DEFAULT 0 NOT NULL")
        add_col("users", "notice_version", "notice_version INTEGER DEFAULT 0 NOT NULL")
        add_col("users", "reg_note", "reg_note VARCHAR(300) DEFAULT '' NOT NULL")
        add_col("users", "last_checkin_key", "last_checkin_key VARCHAR(10) DEFAULT '' NOT NULL")
        add_col("users", "max_inflight", "max_inflight INTEGER")
        # 仅在建列当次回填注册备注；之后不再动（避免管理员清空后重启被还原）
        if add_col("users", "note", "note VARCHAR(500) DEFAULT '' NOT NULL"):
            sync_conn.execute(text("UPDATE users SET note = reg_note WHERE reg_note != ''"))
        indexes = {i["name"] for i in inspector.get_indexes("users")}
        if "ix_users_reg_fp" not in indexes:
            sync_conn.execute(text("CREATE INDEX ix_users_reg_fp ON users (reg_fp)"))

    if "user_quotas" in inspector.get_table_names():
        add_col("user_quotas", "temp_amount", "temp_amount INTEGER DEFAULT 0 NOT NULL")
        add_col("user_quotas", "temp_expires_at", "temp_expires_at TIMESTAMP")  # PG 无 DATETIME 类型

    if "invite_codes" in inspector.get_table_names():
        add_col("invite_codes", "pool_type", "pool_type VARCHAR(16) DEFAULT 'permanent' NOT NULL")
        add_col("invite_codes", "valid_days", "valid_days INTEGER DEFAULT 1 NOT NULL")
        add_col("invite_codes", "valid_hours", "valid_hours INTEGER DEFAULT 0 NOT NULL")
        add_col("invite_codes", "fixed_expires_at", "fixed_expires_at TIMESTAMP")

    if "redemption_codes" in inspector.get_table_names():
        add_col("redemption_codes", "fixed_expires_at", "fixed_expires_at TIMESTAMP")

    # redemptions 表结构调整（invite_code_id → redemption_code_id）：
    # 旧表重命名为 redemptions_legacy 留存，按新结构重建（历史兑换记录可手工迁移）
    if "redemptions" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("redemptions")}
        if "redemption_code_id" not in cols and "redemptions_legacy" not in inspector.get_table_names():
            from . import models

            sync_conn.execute(text("ALTER TABLE redemptions RENAME TO redemptions_legacy"))
            models.Redemption.__table__.create(bind=sync_conn)
