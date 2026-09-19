# 上游 chatgpt2api 升级与融合指南

PicSystem 与上游 chatgpt2api 之间通过其 **OpenAI 兼容 HTTP 契约** 集成。
本文说明该契约的依赖范围、升级步骤、兼容性验证与回滚方法。

## 1. 契约依赖面

PicSystem 只使用上游以下接口（除 `/files/*` 外均带 `Authorization: Bearer <上游密钥>`）：

| 上游接口 | PicSystem 用途 |
| :--- | :--- |
| `GET /v1/models` | 模型目录、后台连通性测试 |
| `POST /v1/chat/completions` | 对话（SSE 流式 / 非流式） |
| `POST /v1/search` `{prompt}` | 联网搜索 |
| `POST /v1/images/generations` | 文生图（请求 `response_format=b64_json`） |
| `POST /v1/images/edits`（multipart，字段 `image`，可选 `mask`） | 图生图 / 局部编辑（遮罩重绘） |
| `POST /v1/ppt/generations`、`POST /v1/psd/generations` | 创建 PPT/PSD 任务 |
| `GET /v1/editable-file-tasks?ids=` | 任务状态轮询（`items[].status/result.primary_url`） |
| `GET /files/{path}` | 下载上游产物（仅在响应返回 URL 形式时） |

解析策略上对字段缺失做了宽容处理（如 `result.primary_url` 与 `zip_url` 二选一、
`b64_json` 与 `url` 两种响应都支持），上游做小版本演进时通常无需改动。

## 2. 升级内置上游（compose 内置模式）

```bash
docker compose pull chatgpt2api
docker compose up -d
```

固定版本（推荐生产环境，避免上游 breaking change 突然影响）：

```env
# .env
CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:v3.2.3
```

改完后 `docker compose up -d`。可用版本号见上游
[releases](https://github.com/yukkcat/chatgpt2api/releases)。

> 上游数据（账号、设置）保存在 `./data/chatgpt2api` 与 `chatgpt2api-runtime` 卷，
> 升级镜像不会丢失。但请留意上游 release note 中的迁移说明（如 2.x → 3.x 的数据库变更）。

## 3. 升级外部上游（外部 URL 模式）

外部上游由你自行运维，按上游官方方式升级即可。升级后 PicSystem 无需变更，
只需按下一节做兼容性验证。

## 4. 升级后兼容性验证（约 2 分钟）

1. 管理后台 → 系统设置 → 上游服务 → **测试连接**：应显示 `连接正常 · 延迟 xx ms · N 个模型`。
2. **查看模型目录**：确认模型 id 列表正常返回。
3. 各功能冒烟一次（后台会记录调用日志，失败可即时看到错误）：
   - 对话：发送一条短消息，能流式出字；
   - 搜索：搜一个简单问题，能返回答案与引用；
   - 绘图：生成 1 张图（n=1），能入图库；
   - PPT：创建一个任务，状态能正常流转（若上游无付费账号会返回对应错误，属上游限制而非兼容问题）。
4. 任一失败：先看 管理后台 → 调用日志 的错误详情，判断是上游问题还是契约变化。

## 5. 契约变化时的处理

上游是逆向项目，接口理论上可能变化。若验证发现某功能因契约变化失效：

1. **回滚上游**（最快恢复）：内置模式把 `.env` 的 `CHATGPT2API_IMAGE` 固定到上一可用版本后
   `docker compose up -d`；外部模式联系上游运维回滚。
2. **临时关闭受影响功能**：管理后台 → 系统设置 → 功能开关，先关闭该功能，用户端随即隐藏入口，
   其余功能不受影响（功能之间无耦合）。
3. **关注 PicSystem 更新**：契约适配会随新版本发布，届时拉取代码重新 `bash deploy.sh`（或
   `docker compose up -d --build`）即可。

## 6. 面板融合边界说明

- **在 PicSystem 面板内完成**：
  - 运营侧：用户/邀请码/额度/存储/日志/风控/功能开关/上游连接配置与测试；
  - 账号侧：上游账号的列表筛选、8 种导入、启停/删除/同步、账号组管理（**代理**上游
    `/api/accounts*` 等管理接口，账号池只有上游一个数据源，不复制状态）；
  - 终端用户的全部使用（对话、绘图、搜索、PPT、文件、API 密钥）。
- **仍只能在上游控制台完成**：账号出口代理与代理组、图片/存储保留策略、注册机、
  实时监控与调试中心。这部分跟随上游版本演进，PicSystem 不复制实现，而是在
  「系统设置 → 上游服务 → 打开上游控制台」提供直达入口（内置模式指向
  `http://127.0.0.1:3000`，外部模式指向你配置的上游地址）。
  内置与外部两种模式下，融合方式完全一致。
- **产物存储边界**：上游生成的图片/PPT/PSD 会被 PicSystem 下载并落到本站存储
  （`StoredFile` + `app-data` 卷），对外只暴露本站 URL（`/api/files/{id}/download`、
  `/v1/files/{id}/download`），终端用户在整条链路上看不到上游地址。
  但两边**各存一份**：PicSystem 的「存储与清理」只统计/清理自己的 `data/files`，
  不会也不应触碰上游容器内的副本，上游那份由上游自己的存储策略管理。
- **单一管理入口约定（建议）**：日常运营只用 PicSystem 面板；需要调出口代理/存储策略时
  再临时开启上游控制台（`.env` 加 `COMPOSE_FILE` 后 `docker compose up -d`），
  用完移除该行并 `docker compose up -d` 恢复零端口暴露。
