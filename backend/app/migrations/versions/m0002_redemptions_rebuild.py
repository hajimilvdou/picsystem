"""redemptions 表结构重建：`invite_code_id` → `redemption_code_id`。

历史数据不删除：旧表整体重命名为 `redemptions_legacy` 留存，
新表按当前模型创建（空表）。需要保留历史兑换记录时按
docs/MIGRATIONS.md 的「数据搬运」小节人工迁移。
"""
from __future__ import annotations

DESCRIPTION = "重建 redemptions 表（invite_code_id → redemption_code_id），旧表留存为 redemptions_legacy"
DESTRUCTIVE = False


def apply(ctx) -> None:
    if not ctx.table_exists("redemptions"):
        return  # 全新库：create_all 会直接建出最新结构
    if ctx.column_exists("redemptions", "redemption_code_id"):
        return  # 已经是新结构
    if ctx.table_exists("redemptions_legacy"):
        return  # 已经迁移过（且旧表已留存）
    if not ctx.rename_table("redemptions", "redemptions_legacy"):
        return
    from ...models import Redemption

    ctx.create_table_from_model(Redemption)
