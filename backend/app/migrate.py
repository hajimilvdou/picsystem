"""迁移命令行。

    python -m app.migrate status              查看每条迁移的状态
    python -m app.migrate up                  应用全部待执行迁移（启动时也会自动执行）
    python -m app.migrate up --dry-run        只列出将要执行的迁移，不改动数据库
    python -m app.migrate up --target 0002    只升到指定序号（含）

容器内用法：`docker compose exec api python -m app.migrate status`
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")

_STATE_LABEL = {
    "applied": "已应用",
    "pending": "待执行",
    "edited": "已改动",
}


async def _status() -> int:
    from .database import engine
    from .migrations import describe_database, migration_status

    try:
        info = await describe_database(engine)
        rows = await migration_status(engine)
    finally:
        await engine.dispose()

    print(f"数据库方言：{info['dialect']}    表数量：{info['table_count']}")
    print("-" * 76)
    print(f"{'序号':<6}{'状态':<8}{'耗时':<8}说明")
    for row in rows:
        applied = row.applied_at.strftime("%Y-%m-%d %H:%M") if row.applied_at else "-"
        flag = "【破坏性】" if row.destructive else ""
        print(f"{row.id:<6}{_STATE_LABEL.get(row.state, row.state):<8}{row.duration_ms:>5}ms {flag}{row.description}")
        if row.state != "pending":
            print(f"{'':<6}{'':<8}{'':<8}应用时间 {applied}")
    pending = [row for row in rows if row.state == "pending"]
    edited = [row for row in rows if row.state == "edited"]
    print("-" * 76)
    print(f"共 {len(rows)} 条：已应用 {len(rows) - len(pending) - len(edited)}，待执行 {len(pending)}，已改动 {len(edited)}")
    if edited:
        print("！有迁移在应用后被修改，其改动不会生效——请新增一条迁移而不是改旧的（见 docs/MIGRATIONS.md）")
    return 0


async def _up(target: str | None, dry_run: bool) -> int:
    from .database import engine
    from .migrations import run_migrations

    try:
        report = await run_migrations(engine, target=target, dry_run=dry_run)
    finally:
        await engine.dispose()

    if dry_run:
        if not report.pending:
            print("无待执行迁移。")
        else:
            print("将要执行：")
            for label in report.pending:
                print(f"  - {label}")
        return 0

    print(report.summary())
    for item in report.skipped_destructive:
        print(f"跳过（破坏性，MIGRATIONS_SKIP_DESTRUCTIVE 已开启）：{item}")
    for item in report.applied:
        print(f"  · {item.id} {item.description} —— {item.changed} 处改动 / {item.duration_ms}ms")
    if report.edited:
        print("！以下迁移在应用后被修改过，改动未生效：" + "、".join(report.edited))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrate", description="PicSystem 数据库迁移")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="查看迁移状态（默认）")
    up = sub.add_parser("up", help="应用待执行迁移")
    up.add_argument("--target", default=None, help="只升到指定序号，如 0002")
    up.add_argument("--dry-run", action="store_true", help="只列出将要执行的迁移")

    args = parser.parse_args(argv)
    if args.command in (None, "status"):
        return asyncio.run(_status())
    if args.command == "up":
        return asyncio.run(_up(args.target, args.dry_run))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
