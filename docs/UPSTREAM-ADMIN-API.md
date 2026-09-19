# 上游账号管理 API 适配预案（路线 C）

PicSystem 的「上游账号」页（管理后台 → 上游账号）通过上游 chatgpt2api 的**管理 API**
（`/api/accounts*`）实现。该 API 是上游的**内部接口**（区别于有兼容承诺的 `/v1` OpenAI 契约），
上游版本更新可能改变其字段或行为。本文是后续适配指引。

> 相关文档：[docs/UPSTREAM.md](./UPSTREAM.md)（`/v1` 契约与上游升级/回滚总流程）。

## 1. 依赖面（当前按 v3.2.3 适配）

鉴权：全部 `Authorization: Bearer <CHATGPT2API_AUTH_KEY>`（即 PicSystem 的 UPSTREAM_API_KEY）。

### 1.1 账号列表与管理

| PicSystem 功能 | 上游端点 | 关键请求字段 | 关键响应字段 |
| :--- | :--- | :--- | :--- |
| 账号列表 | `GET /api/accounts` | query: `page/page_size/keyword/status/group_id` | `items[]`、`total` |
| 添加 / 导入账号 | `POST /api/accounts` | `tokens[]`、`accounts[]`、`sync_after_import`、`restore`、`target_group_id`、`return_items` | `added/skipped/synced/updated_ids/errors[]/events[]` |
| 启用/禁用 | `POST /api/accounts/batch-update` | `account_ids[]`、`operation(enable/disable)`、`status(正常/禁用)` | `progress_id`（异步） |
| 删除 | `DELETE /api/accounts` | body: `account_ids[]` | `progress_id`（异步） |
| 同步额度 | `POST /api/accounts/sync` | `account_ids[]`（空=全部） | `progress_id`（异步） |
| 导入后清理失效账号 | `POST /api/accounts/import-cleanup` | `account_ids[]`、`remove(bool)` | `checked/abnormal/removed/removed_ids` |
| 进度轮询 | `GET /api/accounts/operations/{progress_id}` | — | `done/processed/total/result` |
| 账号组列表 | `GET /api/account-groups` | — | `groups[]`、`proxy_groups[]` |
| 新建/更新账号组 | `POST /api/account-groups` | `id/name/enabled/notes` | `groups[]` |
| 删除账号组 | `DELETE /api/account-groups/{group_id}` | — | `groups[]`、`updated_ids/removed_ids` |
| 批量绑定分组 | `POST /api/accounts/group` | `account_ids[]`、`group_id`（`__ungrouped__`=移出） | `group_id/updated_ids` |

### 1.2 导入模式与凭据口径

PicSystem「导入账号」与上游控制台的 8 种模式一一对应，凭据解析口径完全复用上游实现
（`web-vue/src/views/accounts/accountImportRuntime.ts`）：

| 模式 | 上游依赖 | 说明 |
| :--- | :--- | :--- |
| 导入 Access Token | `POST /api/accounts`（`tokens[]`） | 一行一个；忽略空行与 `#` 注释行；支持 TXT 文件 |
| 导入 Session JSON | `POST /api/accounts`（`accounts[]`） | 兼容 `chatgpt.com/api/auth/session` 返回的 JSON |
| 导入完整备份文件 | `POST /api/accounts`（`accounts[]` + `restore=true`） | 不请求远程验证，直接恢复凭据与配置 |
| 导入 CPA / Sub2API JSON 文件 | `POST /api/accounts`（`accounts[]`） | 浏览器侧解析后按账号负载提交 |
| OAuth 登录已有账号 | `POST /api/accounts/oauth/start`、`/oauth/finish` | 换取 RT 用于 AT 临期自动续期 |
| 从远程 CPA 导入 | `GET /api/cpa/pools`、`GET /api/cpa/pools/{id}/files`、`POST /api/cpa/pools/{id}/import`、`GET /api/cpa/pools/{id}/import` | 文件勾选 → 异步任务 |
| 从 Sub2API 导入 | `GET /api/sub2api/servers`、`/groups`、`/accounts`、`POST /api/sub2api/servers/{id}/import`、`GET .../import` | 支持按远端分组创建/复用同名账号组 |

所有 JSON 文件导入共用同一套容错解析：记录可位于数组根、`accounts / items / results / data` 等
常见容器下，凭据可在记录根或 `credentials / credential / tokens / auth` 子对象中，
字段别名 `access_token / accessToken / token`（另兼容 `refresh_token`、`id_token`）。

异步远程导入统一返回 `import_job`（`job_id/status/stage/stage_label/terminal/progress_total/progress_completed/result_message/result_tone/errors[]/events[]`），
PicSystem 以 1.5s 间隔轮询并在重新打开弹窗时自动恢复跟踪未完成任务。

账号列表项（`items[]`）使用的展示字段：
`id / email / user_id / display_name / plan_label / source_label / status_label / status_tone /
status_reason / quota_label / quota_reset_at / image_inflight /
access_token_label / access_token_tone / refresh_token_label /
success_count / failure_count / enabled / group_id / group_name / last_used_at（秒级时间戳）`。

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

代理实现集中在三处，改动面很小：

- 后端：`backend/app/routers/admin_upstream.py`（端点映射、白名单投影与审计）
- 前端页面：`frontend/src/views/admin/UpstreamAccountsView.vue`（列表、批量操作、导入弹窗）
- 前端解析：`frontend/src/utils/accountImport.js`（Token 行 / Session JSON / 备份 JSON 解析）

适配步骤：

1. 对照上游新版本 `api/accounts.py`、`api/support.py` 中的端点与字段，更新 `admin_upstream.py` 的
   路径/请求体/字段白名单（多数情况是字段重命名或新增字段）。
2. 字段白名单是三组常量：`_ACCOUNT_ITEM_FIELDS`、`_ACCOUNT_GROUP_FIELDS` 与各 `_CPA_* / _SUB2API_* /
   _IMPORT_JOB_* / _MUTATION_FIELDS`。上游新增展示字段时必须显式加白名单，否则前端拿不到。
3. 若上游改动了导入解析规则，同步更新 `frontend/src/utils/accountImport.js`（与
   `web-vue/src/views/accounts/accountImportRuntime.ts` 对齐）。
4. 运行 `cd backend && python smoke_test.py` 回归；手动在「上游账号」页验证列表/导入/启停/删除/同步/分组。
5. 更新本文档第 1 节的版本号与字段表。

## 4. 设计约定（适配时勿破坏）

- 上游密钥只存服务端，任何上游响应中的敏感字段（token 原文、代理地址、CPA `secret_key`、
  Sub2API `password/api_key`）**不得**透传到前端。后端每个响应都走白名单投影
  （账号负载里的 token 只在「导入」方向上行传输，永不下发），新增字段时须逐一确认。
- 所有写操作（添加/导入/OAuth/启停/删除/同步/分组/远程连接）必须写审计日志（`audit()`），
  且审计明细不得包含任何凭据原文。
- 异步操作统一走 `progress_id` 或 `import_job` + 轮询模式，不要在请求内长等。
- **导出（`POST /api/accounts/export`）故意不代理**：上游该接口会把 access token 原文
  （json/txt）或整包（zip）下发给浏览器，与「密钥不出服务端」的设计前提冲突。
  需要导出时请走上游控制台临时通道（见第 2 节），不要在 PicSystem 里补这个代理。
