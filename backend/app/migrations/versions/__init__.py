"""版本化迁移脚本目录。

命名约定：`m<4位序号>_<小写_下划线名称>.py`，例如 `m0003_add_user_language.py`。
序号决定执行顺序（升序、全局唯一），**已发布的序号不可复用、内容不可再改**。
每个模块需暴露：

    DESCRIPTION = "一行说明"      # 会写进 schema_migrations 表
    DESTRUCTIVE = False          # 可选；含删表/删列时置 True
    def apply(ctx) -> None: ...   # 迁移主体，必须幂等（见 docs/MIGRATIONS.md）

新增迁移的完整流程见 docs/MIGRATIONS.md。
"""
