"""图库标签：stored_files 增加 tags 列（逗号分隔）。"""
from __future__ import annotations

DESCRIPTION = "stored_files 增加 tags 列（图库标签）"
DESTRUCTIVE = False


def apply(ctx) -> None:
    ctx.add_column("stored_files", "tags", "tags VARCHAR(300) DEFAULT '' NOT NULL")
