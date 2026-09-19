"""迁移执行器：版本发现、跟踪表、串行锁、逐条事务、状态查询。

执行时机：`app.database.init_db()` 在应用启动时调用 `run_migrations()`，
所以**拉取新版本后重启容器即自动完成迁移**，无需人工执行 SQL。

顺序（重要）：
    1. 建跟踪表 schema_migrations
    2. 跑完所有待执行迁移（只作用于「历史库已存在的表」，全新库为空跑）
    3. Base.metadata.create_all（建出所有缺失的新表，直接是最新结构）
    4. seed_defaults（默认设置 / 初始管理员）

安全性：
- 每条迁移独立事务：PostgreSQL / SQLite 的 DDL 都可回滚，失败即整条撤销并抛出，
  应用**不会带着半截 schema 启动**；
- PostgreSQL 下用 advisory lock 串行化，避免多实例同时迁移；
- 记录 checksum：已应用的迁移若被事后修改，`status` 会标为 EDITED 并告警。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import pkgutil
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    false,
    inspect,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from ..config import settings
from .context import MigrationContext, MigrationError

log = logging.getLogger("picsystem.migrations")

_MODULE_PATTERN = re.compile(r"^m(\d{4})_([a-z0-9_]+)$")

# advisory lock 的固定 key（"PICS"），保证同一库上只有一个迁移在跑
_LOCK_KEY = 0x50494353


def _lock_timeout_seconds() -> float:
    """等待迁移锁的上限（秒）。读配置而非环境变量，才能同时支持 .env 与 compose 注入。"""
    try:
        return max(1.0, float(settings.migrations_lock_timeout_seconds))
    except (TypeError, ValueError):
        return 120.0

_metadata = MetaData()

schema_migrations = Table(
    "schema_migrations",
    _metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(120), nullable=False, server_default=""),
    Column("description", String(300), nullable=False, server_default=""),
    Column("checksum", String(64), nullable=False, server_default=""),
    Column("applied_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False, server_default="0"),
    Column("destructive", Boolean, nullable=False, server_default=false()),
)


@dataclass(frozen=True)
class Migration:
    """一条版本化迁移。由 versions/m<NNNN>_<name>.py 自动装配。"""

    id: str
    name: str
    description: str
    checksum: str
    destructive: bool
    apply: Callable[[MigrationContext], None]
    source: str

    @property
    def label(self) -> str:
        return f"{self.id}_{self.name}"


@dataclass
class AppliedMigration:
    id: str
    name: str
    description: str
    duration_ms: int
    changed: int
    destructive: bool = False


@dataclass
class MigrationReport:
    applied: list[AppliedMigration] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    skipped_destructive: list[str] = field(default_factory=list)
    dry_run: bool = False
    duration_ms: int = 0

    @property
    def total_changes(self) -> int:
        return sum(item.changed for item in self.applied)

    def summary(self) -> str:
        if self.dry_run:
            return f"待执行 {len(self.pending)} 条（dry-run，未改动数据库）"
        if not self.applied:
            return "无待执行迁移"
        detail = "、".join(f"{item.id}（{item.changed} 处改动）" for item in self.applied)
        return f"已应用 {len(self.applied)} 条：{detail}，耗时 {self.duration_ms}ms"


@dataclass
class MigrationStatusRow:
    id: str
    name: str
    description: str
    state: str  # applied / pending / edited
    applied_at: datetime | None = None
    duration_ms: int = 0
    destructive: bool = False


# ------------------------------------------------------------------ 版本发现


def load_migrations() -> list[Migration]:
    """扫描 `versions/` 下 `m<4位序号>_<名称>.py`，按文件名升序返回。

    每个版本模块需要暴露：
        DESCRIPTION: str          # 一行说明，会写进跟踪表
        DESTRUCTIVE: bool = False # 是否含删表/删列/截断等不可逆操作
        def apply(ctx: MigrationContext) -> None: ...
    """
    from . import versions

    found: dict[str, Migration] = {}
    for module_info in pkgutil.iter_modules(versions.__path__):
        module_name = module_info.name
        matched = _MODULE_PATTERN.match(module_name)
        if not matched:
            if module_info.ispkg or module_name == "__init__":
                continue  # 子包不是迁移
            if module_name.startswith("m") and module_name[1:2].isdigit():
                # 形如 m3_xxx / m0003-add-xxx 明显想当迁移，但不符合命名规范。
                # 静默跳过会变成「写了迁移却没执行」的隐性事故，这里直接报错。
                raise MigrationError(
                    f"versions/{module_name}.py 命名不符合规范，应为 "
                    f"m<4位序号>_<小写名称>.py（例如 m0003_add_user_language.py）"
                )
            log.warning("versions/%s.py 不是迁移脚本（命名不匹配），已忽略", module_name)
            continue
        number, name = matched.group(1), matched.group(2)
        module = importlib.import_module(f"{versions.__name__}.{module_info.name}")
        apply_fn = getattr(module, "apply", None)
        if not callable(apply_fn):
            raise MigrationError(f"{module_info.name} 缺少 apply(ctx) 函数")
        source = getattr(module, "__file__", "") or ""
        checksum = _checksum_of(source)
        if number in found:
            raise MigrationError(f"迁移序号 {number} 重复：{found[number].name} / {name}")
        found[number] = Migration(
            id=number,
            name=name,
            description=str(getattr(module, "DESCRIPTION", "") or name),
            checksum=checksum,
            destructive=bool(getattr(module, "DESTRUCTIVE", False)),
            apply=apply_fn,
            source=source,
        )
    return [found[key] for key in sorted(found)]


def _checksum_of(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return ""


# ------------------------------------------------------------------ 跟踪表


async def _ensure_tracking_table(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_metadata.create_all)


async def _read_applied(engine: AsyncEngine) -> dict[str, str]:
    """返回 {migration_id: checksum}。"""
    async with engine.connect() as conn:
        rows = (await conn.execute(select(schema_migrations.c.id, schema_migrations.c.checksum))).all()
    return {row[0]: row[1] for row in rows}


# ------------------------------------------------------------------ 串行锁


async def _acquire_lock(conn, dialect: str) -> None:
    if dialect != "postgresql":
        return
    timeout = _lock_timeout_seconds()
    deadline = time.monotonic() + timeout
    while True:
        acquired = (await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY})).scalar()
        if acquired:
            # 立刻结束隐式事务：advisory lock 是会话级，提交后依然持有，
            # 这样迁移期间不会留下长时间空闲的事务
            await conn.commit()
            return
        if time.monotonic() >= deadline:
            raise MigrationError(
                f"等待迁移锁超过 {timeout:.0f} 秒，可能另一个实例正在迁移。"
                f"确认无其他实例在跑后重启即可，或调整 MIGRATIONS_LOCK_TIMEOUT_SECONDS。"
            )
        await asyncio.sleep(0.5)


async def _release_lock(conn, dialect: str) -> None:
    if dialect != "postgresql":
        return
    try:
        await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
        await conn.commit()
    except Exception as exc:  # noqa: BLE001 - 释放失败不能掩盖迁移本身的异常
        log.warning("释放迁移锁失败（连接可能已断开，锁会随会话结束自动释放）：%s", exc)


# ------------------------------------------------------------------ 执行


def _run_sync_migration(sync_conn, migration: Migration, dialect: str, holder: dict) -> None:
    ctx = MigrationContext(sync_conn, dialect)
    migration.apply(ctx)
    holder["changed"] = ctx.changed


async def run_migrations(
    engine: AsyncEngine,
    *,
    target: str | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """应用所有待执行迁移。`target` 可指定只升到某个序号（含）。"""
    started = time.perf_counter()
    report = MigrationReport(dry_run=dry_run)
    all_migrations = load_migrations()
    if not all_migrations:
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        return report

    await _ensure_tracking_table(engine)
    dialect = engine.dialect.name

    async with engine.connect() as lock_conn:
        await _acquire_lock(lock_conn, dialect)
        try:
            applied_map = await _read_applied(engine)
            report.edited = [
                item.id
                for item in all_migrations
                if item.id in applied_map and applied_map[item.id] != item.checksum
            ]
            pending = [item for item in all_migrations if item.id not in applied_map]
            if target:
                pending = [item for item in pending if item.id <= target]
            if settings.migrations_skip_destructive:
                report.skipped_destructive = [item.label for item in pending if item.destructive]
                pending = [item for item in pending if not item.destructive]
            report.pending = [item.label for item in pending]

            for item in report.edited:
                log.warning(
                    "迁移 %s 的内容在应用后被修改过，改动不会生效；"
                    "请改为新增一条迁移（详见 docs/MIGRATIONS.md）",
                    item,
                )

            if dry_run or not pending:
                report.duration_ms = int((time.perf_counter() - started) * 1000)
                return report

            for migration in pending:
                if migration.destructive:
                    log.warning("迁移 %s 是破坏性操作（删表/删列），即将执行", migration.label)

                item_started = time.perf_counter()
                holder: dict = {}
                async with engine.begin() as conn:
                    await conn.run_sync(_run_sync_migration, migration, dialect, holder)
                    await conn.execute(
                        schema_migrations.insert().values(
                            id=migration.id,
                            name=migration.name,
                            description=migration.description[:300],
                            checksum=migration.checksum,
                            applied_at=datetime.now(timezone.utc),
                            duration_ms=0,
                            destructive=migration.destructive,
                        )
                    )
                cost = int((time.perf_counter() - item_started) * 1000)
                try:
                    # 耗时用第二条小事务补写（迁移已提交）；即便补写失败也不能影响启动
                    async with engine.begin() as conn:
                        await conn.execute(
                            schema_migrations.update()
                            .where(schema_migrations.c.id == migration.id)
                            .values(duration_ms=cost)
                        )
                except Exception as exc:  # noqa: BLE001 - 仅记录耗时，不阻断
                    log.warning("迁移 %s 耗时回写失败（不影响结果）：%s", migration.label, exc)
                report.applied.append(
                    AppliedMigration(
                        id=migration.id,
                        name=migration.name,
                        description=migration.description,
                        duration_ms=cost,
                        changed=int(holder.get("changed") or 0),
                        destructive=migration.destructive,
                    )
                )
                log.info(
                    "[迁移] %s %s 完成：%d 处改动，%dms",
                    migration.id,
                    migration.description,
                    holder.get("changed") or 0,
                    cost,
                )
        finally:
            await _release_lock(lock_conn, dialect)

    report.duration_ms = int((time.perf_counter() - started) * 1000)
    return report


# ------------------------------------------------------------------ 状态查询


async def migration_status(engine: AsyncEngine) -> list[MigrationStatusRow]:
    await _ensure_tracking_table(engine)
    applied = await _read_applied(engine)
    rows: list[MigrationStatusRow] = []
    async with engine.connect() as conn:
        recorded = {
            row[0]: row
            for row in (
                await conn.execute(
                    select(
                        schema_migrations.c.id,
                        schema_migrations.c.checksum,
                        schema_migrations.c.applied_at,
                        schema_migrations.c.duration_ms,
                    )
                )
            ).all()
        }
    for item in load_migrations():
        row = recorded.get(item.id)
        if row is None:
            state = "pending"
        elif row[1] != item.checksum:
            state = "edited"
        else:
            state = "applied"
        rows.append(
            MigrationStatusRow(
                id=item.id,
                name=item.name,
                description=item.description,
                state=state,
                applied_at=row[2] if row else None,
                duration_ms=int(row[3] or 0) if row else 0,
                destructive=item.destructive,
            )
        )
    return rows


async def describe_database(engine: AsyncEngine) -> dict:
    """给状态输出附带的库信息。"""
    dialect = engine.dialect.name
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: sorted(inspect(sync_conn).get_table_names()))
    return {"dialect": dialect, "tables": tables, "table_count": len(tables)}
