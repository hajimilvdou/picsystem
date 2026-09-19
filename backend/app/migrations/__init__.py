"""数据库迁移框架：版本化、幂等、随应用启动自动执行。

对使用者（运维）而言，**更新只需 `git pull` + 重新部署**：`app.database.init_db()`
会在应用启动时自动跑完所有待执行迁移，失败则启动中止（不会带着半截 schema 运行）。
想单独查看状态或手动执行：

    docker compose exec api python -m app.migrate status
    docker compose exec api python -m app.migrate up

对开发者（新增迁移）请看 docs/MIGRATIONS.md。
"""
from .context import MigrationContext, MigrationError
from .runner import (
    AppliedMigration,
    Migration,
    MigrationReport,
    MigrationStatusRow,
    describe_database,
    load_migrations,
    migration_status,
    run_migrations,
)

__all__ = [
    "AppliedMigration",
    "Migration",
    "MigrationContext",
    "MigrationError",
    "MigrationReport",
    "MigrationStatusRow",
    "describe_database",
    "load_migrations",
    "migration_status",
    "run_migrations",
]
