"""迁移脚本可用的 DDL 工具。

规则（写迁移前必读 docs/MIGRATIONS.md）：
- **全部 helper 幂等**：先检查现状再执行，重复运行安全。
- **表不存在时一律跳过**：全新库由 `Base.metadata.create_all` 直接建成最新结构，
  迁移只需要负责把「历史库的既有表」补到最新，因此表不存在 = 无事可做。
- **双方言**：PostgreSQL（生产）与 SQLite（本地/冒烟测试）都要能跑；
  需要分叉时用 `ctx.dialect` 判断，差异点见各 helper 的注释。
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

log = logging.getLogger("picsystem.migrations")


class MigrationError(RuntimeError):
    """迁移脚本自身的问题（写错了、方言不支持等）。"""


class MigrationContext:
    """传给每条迁移的执行上下文。"""

    def __init__(self, conn, dialect: str) -> None:
        self.conn = conn
        self.dialect = dialect
        self._changed = 0

    # ------------------------------------------------------------------ 基础

    @property
    def changed(self) -> int:
        """本次迁移真正执行的 DDL 条数（重复运行时为 0，便于日志判断是否空跑）。"""
        return self._changed

    def execute(self, sql: str, **params) -> None:
        """执行原生 SQL。参数用命名占位符 `:name` 传入。"""
        self.conn.execute(text(sql), params)
        self._changed += 1

    def _inspector(self):
        # 不缓存：迁移过程中 schema 在变，缓存会读到过期元数据
        return inspect(self.conn)

    def table_exists(self, table: str) -> bool:
        return table in self._inspector().get_table_names()

    def table_names(self) -> set[str]:
        return set(self._inspector().get_table_names())

    def column_names(self, table: str) -> set[str]:
        if not self.table_exists(table):
            return set()
        return {c["name"] for c in self._inspector().get_columns(table)}

    def column_exists(self, table: str, column: str) -> bool:
        return column in self.column_names(table)

    def index_exists(self, table: str, name: str) -> bool:
        if not self.table_exists(table):
            return False
        return name in {i["name"] for i in self._inspector().get_indexes(table)}

    # ------------------------------------------------------------ 常用变更

    def add_column(self, table: str, column: str, ddl: str) -> bool:
        """加列。`ddl` 为完整列定义，如 `note VARCHAR(500) DEFAULT '' NOT NULL`。

        注意：带 NOT NULL 时必须同时给 DEFAULT，否则历史行会违反约束。
        """
        if not self.table_exists(table):
            return False
        if self.column_exists(table, column):
            return False
        self.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        log.info("迁移：%s 增加列 %s", table, column)
        return True

    def drop_column(self, table: str, column: str) -> bool:
        """删列（破坏性）。SQLite 需要 3.35+（Python 3.13 自带版本满足）。"""
        if not self.column_exists(table, column):
            return False
        self.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        log.warning("迁移：%s 删除列 %s（数据已丢弃）", table, column)
        return True

    def create_index(self, table: str, name: str, columns: list[str], *, unique: bool = False) -> bool:
        if not self.table_exists(table):
            return False
        if self.index_exists(table, name):
            return False
        missing = [c for c in columns if not self.column_exists(table, c)]
        if missing:
            raise MigrationError(f"{table} 缺少列 {missing}，无法创建索引 {name}")
        keyword = "UNIQUE INDEX" if unique else "INDEX"
        self.execute(f"CREATE {keyword} {name} ON {table} ({', '.join(columns)})")
        log.info("迁移：%s 创建索引 %s", table, name)
        return True

    def drop_index(self, table: str, name: str) -> bool:
        if not self.index_exists(table, name):
            return False
        self.execute(f"DROP INDEX {name}")
        log.warning("迁移：%s 删除索引 %s", table, name)
        return True

    def rename_table(self, old: str, new: str) -> bool:
        """重命名表。目标已存在时不动（防误覆盖）。"""
        if not self.table_exists(old) or self.table_exists(new):
            return False
        self.execute(f"ALTER TABLE {old} RENAME TO {new}")
        log.warning("迁移：表 %s 重命名为 %s（原表保留，数据未删）", old, new)
        return True

    def drop_table(self, table: str) -> bool:
        """删表（破坏性）。"""
        if not self.table_exists(table):
            return False
        self.execute(f"DROP TABLE {table}")
        log.warning("迁移：表 %s 已删除（数据已丢弃）", table)
        return True

    def create_table_from_model(self, model) -> bool:
        """按当前模型建表（已存在则跳过）。用于「重建表」类迁移的第二步。"""
        name = model.__tablename__
        if self.table_exists(name):
            return False
        model.__table__.create(bind=self.conn)
        self._changed += 1
        log.info("迁移：按当前模型创建表 %s", name)
        return True

    def copy_rows(self, table: str, columns: list[str], source_table: str) -> int:
        """把 `source_table` 的同名列数据搬进 `table`，返回搬运行数。"""
        cols = ", ".join(columns)
        result = self.conn.execute(
            text(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {source_table}")
        )
        self._changed += 1
        return int(result.rowcount or 0)

    # ------------------------------------------------- 类型变更 / 表重建

    def alter_column_type(self, table: str, column: str, *, pg_type: str) -> bool:
        """改列类型。仅 PostgreSQL 支持；SQLite 请改用 `rebuild_table`。"""
        if not self.column_exists(table, column):
            return False
        if self.dialect != "postgresql":
            raise MigrationError(
                f"SQLite 不支持直接修改列类型（{table}.{column}）："
                f"请改用 ctx.rebuild_table(...)，示例见 docs/MIGRATIONS.md"
            )
        self.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {pg_type} USING {column}::{pg_type}"
        )
        log.info("迁移：%s.%s 类型改为 %s", table, column, pg_type)
        return True

    def rebuild_table(
        self,
        table: str,
        *,
        new_ddl: str,
        copy_sql: str,
        temp_name: str | None = None,
        drop_old: bool = True,
    ) -> bool:
        """SQLite 专用：按「重命名 → 建新表 → 搬数据 → 删旧表」重建。

        - `new_ddl`：新表的完整列定义（列名与类型），如
          `(id INTEGER PRIMARY KEY, feature VARCHAR(16) NOT NULL, ...)`；
        - `copy_sql`：`INSERT INTO {table} (...) SELECT ... FROM {temp}` 中
          `SELECT` 之后的部分（可用列名做表达式转换，如 `CAST(amount AS TEXT)`）；
        - `drop_old=False` 可把旧表改名保留（`<table>_legacy`）而不是删除。

        PostgreSQL 不需要重建：请用 `alter_column_type` / `add_column` / `drop_column`。
        """
        if self.dialect == "postgresql":
            raise MigrationError("PostgreSQL 不需要 rebuild_table，请用 ALTER 系列 helper")
        if not self.table_exists(table):
            return False
        temp = temp_name or f"{table}__migration_old"
        if self.table_exists(temp):
            raise MigrationError(f"临时表 {temp} 已存在，请先人工确认上一次重建是否中断")
        self.execute(f"ALTER TABLE {table} RENAME TO {temp}")
        self.execute(f"CREATE TABLE {table} {new_ddl}")
        self.execute(f"INSERT INTO {table} SELECT {copy_sql} FROM {temp}")
        if drop_old:
            self.execute(f"DROP TABLE {temp}")
        else:
            self.execute(f"ALTER TABLE {temp} RENAME TO {table}_legacy")
        log.warning("迁移：SQLite 重建表 %s 完成", table)
        return True
