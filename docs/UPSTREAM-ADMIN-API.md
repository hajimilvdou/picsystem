# 上游账号管理 API 适配预案（路线 C）

PicSystem 的「上游账号」页（管理后台 → 上游账号）通过上游 chatgpt2api 的**管理 API**
（`/api/accounts*`）实现。该 API 是上游的**内部接口**（区别于有兼容承诺的 `/v1` OpenAI 契约），
上游版本更新可能改变其字段或行为。本文是后续适配指引。

> 相关文档：[docs/UPSTREAM.md](./UPSTREAM.md)（`/v1` 契约与上游升级/回滚总流程）。

## 1. 依赖面（当前按 v3.2.3 适配）

鉴权：全部 `Authorization: Bearer <CHATGPT2API_AUTH_KEY>`（即 PicSystem 的 UPSTREAM_API_KEY）。

| PicSystem 功能 | 上游端点 | 关键请求字段 | 关键响应字段 |
| :--- | :--- | :--- | :--- |
| 账号列表 | `GET /api/accounts` | query: `page/page_size/keyword/status` | `items[]`、`total` |
| 添加账号 | `POST /api/accounts` | `tokens[]`、`sync_after_import` | `added/skipped/errors[]` |
| 启用/禁用 | `POST /api/accounts/batch-update` | `account_ids[]`、`operation(enable/disable)`、`status(正常/禁用)` | `progress_id`（异步） |
| 删除 | `DELETE /api/accounts` | body: `account_ids[]` | `progress_id`（异步） |
| 同步额度 | `POST /api/accounts/sync` | `account_ids[]`（空=全部） | `progress_id`（异步） |
| 进度轮询 | `GET /api/accounts/operations/{progress_id}` | — | `done/processed/total/result` |

账号列表项（`items[]`）使用的展示字段：
`id / email / user_id / display_name / plan_label / source_label / status_label / status_tone /
quota_label / access_token_label / access_token_tone / refresh_token_label /
success_count / failure_count / enabled / last_used_at（秒级时间戳）`。

错误格式：`{"detail": {"error": "<消息>"}}`，PicSystem 会提取该消息直接展示。

## 2. 版本变化时的降级策略

上游管理 API 变化时，**仅「上游账号」页受影响**，主站全部功能（对话/绘图/搜索/PPT/用户/额度/日志）
走 `/v1` 契约，不受影响。处理顺序：

1. 管理后台 → 系统设置 → 上游服务 → **测试连接**：确认 `/v1` 主链路正常。
2. 「上游账号」页若报错，先用**临时通道**管理账号（零暴露方案的两种方式，见
   管理面板「打开上游控制台」指引或 docs/UPSTREAM.md）：
   - `.env` 加 `COMPOSE_FILE`（Linux/macOS 冒号、Windows 分号）后 `docker compose up -d`，
     本机访问 `http://127.0.0.1:3000`；或 SSH 隧道。
3. 回滚上游镜像到兼容版本（固定 `CHATGPT2API_IMAGE` tag）；
4. 等待 PicSystem 发布适配版本，或按第 3 节自行适配。

## 3. 自行适配指引（开发者）

代理实现集中在两处，改动面很小：

- 后端：`backend/app/routers/admin_upstream.py`（端点映射与字段透传，含审计）
- 前端：`frontend/src/views/admin/UpstreamAccountsView.vue`（字段展示与操作）

适配步骤：

1. 对照上游新版本 `api/accounts.py` 中的端点与字段，更新 `admin_upstream.py` 的
   路径/请求体（多数情况是字段重命名）。
2. 对照 `services/account_view.py` 的投影字段，更新前端表格列（缺失字段用 `row.xxx || '-'` 兜底）。
3. 运行 `cd backend && python smoke_test.py` 回归；手动在「上游账号」页验证列表/添加/启停/删除/同步。
4. 更新本文档第 1 节的版本号与字段表。

## 4. 设计约定（适配时勿破坏）

- 上游密钥只存服务端，任何上游响应中的敏感字段（token 原文、代理地址）**不得**透传到前端；
  当前列表投影本身已脱敏，新增字段时须逐一确认。
- 所有写操作（添加/启停/删除/同步）必须写审计日志（`audit()`）。
- 异步操作统一走 `progress_id` + 轮询模式，不要在请求内长等。
