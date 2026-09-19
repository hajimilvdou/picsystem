# 数据库迁移（版本化 · 随启动自动执行）

PicSystem 用一套**版本化、幂等**的迁移框架管理表结构变更。对运维来说只有一句话：

> **更新只需 `git pull` + 重新部署，升级在 api 容器启动时自动完成，数据不会丢。**

不需要手写 SQL，不需要 `alembic upgrade`，也不需要在更新前清库。

## 1. 运维视角

| 场景 | 操作 |
| :--- | :--- |
| 更新到新版本 | `git pull` → `bash deploy.sh`（提示复用 `.env` 时回车） |
| 看迁移状态 | `docker compose exec api python -m app.migrate status` |
| 手动执行迁移 | `docker compose exec api python -m app.migrate up` |
| 预览将要执行的迁移 | `docker compose exec api python -m app.migrate up --dry-run` |
| 只升到某个序号 | `docker compose exec api python -m app.migrate up --target 0002` |

`deploy.sh` / `deploy.ps1` 在健康检查通过后会直接打印一次迁移状态，所以每次部署都能看到
「本次升级改了什么、耗时多久」。

> `up` 只执行结构迁移；建表与默认数据由应用启动时的 `init_db` 负责。
> 正常升级**不需要**手动跑 `up`，重启容器即可。

已应用的迁移记录在 `schema_migrations` 表：`id / name / description / checksum / applied_at /
duration_ms / destructive`。

两个可选环境变量（写进 `.env` 即可，compose 会注入 api 容器；
它们被声明在 compose 的 api `environment` 里，否则只写在 `.env` 不会进容器）：

| 变量 | 默认 | 作用 |
| :--- | :--- | :--- |
| `MIGRATIONS_LOCK_TIMEOUT_SECONDS` | `120` | PostgreSQL 下等待迁移锁的上限（多实例部署时防并发迁移） |
| `MIGRATIONS_SKIP_DESTRUCTIVE` | 未设置 | 设为 `true` 时跳过标记为破坏性的迁移（谨慎使用） |

## 2. 执行顺序与原理

应用启动（`app.database.init_db`）时依次执行：

1. **建跟踪表** `schema_migrations`（`CREATE TABLE IF NOT EXISTS` 语义）
2. **执行全部待执行迁移**——每条一个独立事务；PostgreSQL 下先用 advisory lock 串行化
3. **`Base.metadata.create_all`**——建出所有缺失的表（直接是最新结构）
4. **`seed_defaults`**——默认设置与初始管理员

**为什么迁移要排在 `create_all` 之前？** 这样两种库都能正确处理：

| 库的状态 | 迁移做什么 | create_all 做什么 |
| :--- | :--- | :--- |
| 全新库 | 全部空跑（表还不存在，helper 自动跳过） | 一次性建出全部最新表 |
| 历史库 | 只改「已存在的表」：补列、建索引、重建表 | 建出本次新增的表 |

失败即中止：迁移抛异常 → 事务回滚 → `init_db` 抛出 → 容器启动失败。
**宁可起不来，也不带着半截表结构对外服务。**

> ⚠️ 长迁移与健康检查：`api` 的 healthcheck 是 `start_period 20s` + 最多 10 次 × 15s，
> 即迁移若超过约 2.5 分钟，容器会被判定为 unhealthy（虽然迁移仍在跑）。
> 涉及大表回填时请分批次写，或临时把 `docker-compose.yml` 里 api 的
> `healthcheck.start_period` 调大再升级。

## 3. 新增一条迁移

### 步骤

1. 新增文件 `backend/app/migrations/versions/m<4位序号>_<小写下划线名称>.py`
   （序号递增且全局唯一，例：`m0003_add_user_language.py`）
   > 命名必须严格匹配。若写成 `m3_xxx.py` 这类"像迁移但格式不对"的文件，
   > 启动时会**直接报错**（而不是静默跳过）——避免出现「写了迁移却没执行」的隐性事故。
2. 写 `DESCRIPTION`、按需写 `DESTRUCTIVE`、实现 `apply(ctx)`
3. 跑 `cd backend && python smoke_test.py` 回归
4. **不要**修改已发布的迁移文件（见第 4 节铁律）

### 骨架

```python
"""给 users 增加 language 字段。"""
from __future__ import annotations

DESCRIPTION = "users 增加 language 字段"
DESTRUCTIVE = False


def apply(ctx) -> None:
    ctx.add_column("users", "language", "language VARCHAR(8) DEFAULT 'zh' NOT NULL")
```

### 示例一：加列 / 建索引（最常用）

```python
def apply(ctx) -> None:
    ctx.add_column("users", "language", "language VARCHAR(8) DEFAULT 'zh' NOT NULL")
    ctx.create_index("users", "ix_users_language", ["language"])
```

- 带 `NOT NULL` 的列**必须同时给 `DEFAULT`**，否则历史行违反约束会直接失败。
- 表不存在时 helper 自动跳过（新库交给 `create_all`）。
- `ctx.add_column` 返回 `True` 表示这次真的建了列，可用于「仅在首次建列时回填数据」：

```python
def apply(ctx) -> None:
    if ctx.add_column("users", "note", "note VARCHAR(500) DEFAULT '' NOT NULL"):
        ctx.execute("UPDATE users SET note = reg_note WHERE reg_note != ''")
```

### 示例二：改列类型（只有 PostgreSQL 支持直改）

```python
def apply(ctx) -> None:
    ctx.alter_column_type("usage_logs", "cost", pg_type="NUMERIC(12, 6)")
```

SQLite 不支持改类型，调用会抛 `MigrationError` 并提示改用 `rebuild_table`；
本地/冒烟测试环境如果想跳过，可写成：

```python
def apply(ctx) -> None:
    if ctx.dialect == "sqlite":
        return  # 本地 SQLite 不做类型变更，生产 PG 才需要
    ctx.alter_column_type("usage_logs", "cost", pg_type="NUMERIC(12, 6)")
```

### 示例三：重建表（改结构 / 删列，尤其是 SQLite）

```python
def apply(ctx) -> None:
    if ctx.dialect != "sqlite":
        # PostgreSQL 用标准 ALTER 即可
        ctx.add_column("foo", "bar", "bar INTEGER")
        return
    ctx.rebuild_table(
        "foo",
        new_ddl="(id INTEGER PRIMARY KEY, feature VARCHAR(16) NOT NULL, amount INTEGER, bar INTEGER)",
        copy_sql="id, feature, amount, NULL",
        drop_old=False,   # 旧表改名保留为 foo_legacy，而不是删除
    )
```

### 示例四：数据搬运（保留历史记录）

历史数据不删只留（如 `redemptions_legacy`）时，需要人工搬运，标准写法：

```python
def apply(ctx) -> None:
    if not ctx.table_exists("redemptions_legacy"):
        return
    ctx.execute(
        "INSERT INTO redemptions (redemption_code_id, user_id, created_at)"
        " SELECT invite_code_id, user_id, created_at FROM redemptions_legacy"
        " WHERE invite_code_id IS NOT NULL"
    )
```

### 可用 helper 一览（都在 `app/migrations/context.py`）

| helper | 说明 |
| :--- | :--- |
| `ctx.execute(sql, **params)` | 原生 SQL（参数用 `:name`） |
| `ctx.table_exists` / `table_names` | 表存在性 |
| `ctx.column_exists` / `column_names` | 列存在性 |
| `ctx.index_exists` | 索引存在性 |
| `ctx.add_column(table, col, ddl)` | 加列（幂等，返回是否新建） |
| `ctx.drop_column(table, col)` | 删列（破坏性） |
| `ctx.create_index(table, name, cols, unique=False)` | 建索引 |
| `ctx.drop_index(table, name)` | 删索引 |
| `ctx.rename_table(old, new)` | 重命名（目标已存在则不动） |
| `ctx.drop_table(table)` | 删表（破坏性） |
| `ctx.create_table_from_model(model)` | 按当前模型建表 |
| `ctx.copy_rows(table, cols, source_table)` | 同名列搬运，返回行数 |
| `ctx.alter_column_type(table, col, pg_type=...)` | 改类型（仅 PG） |
| `ctx.rebuild_table(...)` | 重建表（SQLite 专用） |
| `ctx.dialect` / `ctx.changed` | 方言名 / 本次改动条数 |

## 4. 三条铁律

1. **必须幂等**：同一条迁移可能因为在历史库上重跑、或失败后重启而被执行多次。
   所有 helper 已经做了存在性检查，但**自己写的裸 SQL 也要自己保证**（用 `IF NOT EXISTS`、
   `WHERE` 条件或先查状态）。
2. **不要修改已发布的迁移**：框架用 `checksum` 比对文件内容，应用后再改动会被标记为
   `EDITED` 并在启动日志/`status` 里告警，且**改动不会生效**。需要调整就新增一条。
3. **破坏性操作要标注**：删表 / 删列 / 截断数据时设 `DESTRUCTIVE = True`，
   启动日志会打醒目 warn，运维也能用 `MIGRATIONS_SKIP_DESTRUCTIVE` 兜底。

## 5. PostgreSQL / SQLite 差异

生产是 PostgreSQL 18，本地开发与 `smoke_test.py` 用 SQLite，**两条路径都要能跑通**。

| 操作 | PostgreSQL | SQLite |
| :--- | :--- | :--- |
| 加列 / 删列 | `ALTER TABLE` | `ALTER TABLE`（删列需 3.35+，Python 3.13 满足） |
| 改列类型 | `ALTER COLUMN ... TYPE ... USING ...` | 不支持，需 `rebuild_table` |
| 重命名表 | `ALTER TABLE ... RENAME TO` | 同左 |
| DDL 事务 | 可回滚 | 可回滚 |
| 并发迁移 | advisory lock 串行化 | 单进程假设，不加锁 |

注意：SQLite 没有 `DATETIME`，时间列统一写 `TIMESTAMP`；布尔列用 `BOOLEAN DEFAULT false`
（SQLAlchemy 会按方言渲染）。

## 6. 出问题的处理

**迁移失败 → 容器起不来**，日志里会有 `[迁移]` 前缀的报错。处理顺序：

1. 看 api 日志定位是哪条迁移、哪句 SQL：`docker compose logs api | grep -A5 '迁移'`
2. 看状态：`docker compose exec api python -m app.migrate status`（能起容器时）
3. 常见原因：
   - 新增 `NOT NULL` 列没给 `DEFAULT` → 历史行违反约束
   - 历史数据里有脏值（如重复键）导致建唯一索引失败
   - SQLite 上用了 `alter_column_type`
4. **恢复手段**：这条迁移的事务已回滚，数据库仍是升级前的状态，
   因此**修好迁移文件后重启即可**；如果连表结构都乱了，用部署前的备份恢复：

```bash
docker compose stop api
docker compose exec -T db psql -U picsystem -d picsystem < backup.sql
docker compose up -d
```

**回滚整个版本升级**：本项目不做「向下迁移」（维护成本高且容易误删数据）。
回滚 = 恢复数据库备份 + 把代码切回上一个 tag。

## 7. 发布前 checklist

- [ ] 新迁移文件名序号递增且未重复
- [ ] `apply()` 在「表不存在」和「列已存在」两种情况下都不会报错（幂等）
- [ ] `NOT NULL` 新列都有 `DEFAULT`
- [ ] 破坏性操作标了 `DESTRUCTIVE = True`
- [ ] 迁移引入的列/表，`app/models.py` 里也要有对应定义
      （迁移只负责「老库补齐」，新装由 `create_all` 建，两边必须一致）
- [ ] 同步更新 `smoke_test.py` 里的 `migration_managed_columns`，
      让「老库一键升级」用例覆盖本次改动（它会把老库升级结果与全新库结构做全等比对）
- [ ] `cd backend && python smoke_test.py` 通过（含 6 条迁移用例）
- [ ] 未修改任何已发布的迁移文件
