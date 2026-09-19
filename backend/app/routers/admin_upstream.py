"""管理端：上游 chatgpt2api 账号管理（路线 C——密钥不出服务端，上游零端口暴露）。

代理上游管理 API（/api/accounts*、/api/account-groups、/api/cpa/*、/api/sub2api/*）。
该 API 属上游内部契约（区别于稳定的 /v1 兼容契约），适配与降级预案见
docs/UPSTREAM-ADMIN-API.md。

设计约定（改动时勿破坏）：
- 所有响应都按白名单投影，只透传前端展示需要的字段，防上游新增敏感字段穿透；
- 上游的 secret_key / password / api_key / token 原文一律不落到浏览器；
- 所有写操作写审计日志；异步操作只返回 progress_id / import_job，由前端轮询。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import User
from ..services.upstream import request_json, upstream_config
from ..services.usage import audit

router = APIRouter(prefix="/api/admin/upstream-accounts", tags=["admin-upstream"], dependencies=[Depends(require_admin)])

# ---------------------------------------------------------------- 字段白名单

# 账号投影白名单（见 docs/UPSTREAM-ADMIN-API.md §4）
_ACCOUNT_ITEM_FIELDS = {
    "id", "email", "user_id", "display_name", "plan", "plan_label", "source", "source_label",
    "backend_status", "status_category", "status_label", "status_tone", "status_reason",
    "enabled", "available", "access_token_status", "access_token_label", "access_token_tone",
    "refresh_token_status", "refresh_token_label", "refresh_token_tone",
    "quota_remaining", "quota_label", "quota_state", "quota_reset_at",
    "group_id", "group_name", "proxy_label",
    "success_count", "failure_count", "image_inflight",
    "created_at", "last_used_at",
}

_ACCOUNT_GROUP_FIELDS = {"id", "name", "enabled", "notes", "account_count", "proxy_label"}

_CPA_POOL_FIELDS = {"id", "name", "base_url"}
_CPA_FILE_FIELDS = {"name", "email"}

_SUB2API_SERVER_FIELDS = {"id", "name", "base_url", "email", "group_id", "has_api_key", "group_name"}
_SUB2API_ACCOUNT_FIELDS = {
    "id", "name", "email", "plan_type", "status", "expires_at",
    "has_access_token", "has_refresh_token", "remote_group_id", "remote_group_name",
}
_SUB2API_GROUP_FIELDS = {
    "id", "name", "description", "platform", "status", "account_count", "active_account_count",
}

_EVENT_FIELDS = {"sequence", "timestamp", "account_id", "account_label", "action", "status", "tone", "message"}
_ERROR_FIELDS = {"id", "code", "message", "stage", "name", "error"}

_IMPORT_JOB_FIELDS = {
    "job_id", "status", "stage", "stage_label", "stage_total", "stage_completed",
    "terminal", "progress_total", "progress_completed", "status_label", "tone", "error",
    "summary_items", "result_message", "result_tone",
    "total", "completed", "added", "skipped", "synced", "failed", "failed_total",
    "created_at", "updated_at",
}

_MUTATION_FIELDS = {
    # progress_id 是异步写操作（batch-update / delete / sync）的轮询凭据，前端靠它拉进度；
    # 漏掉会让这些操作"点了没反应"（见 docs/UPSTREAM-ADMIN-API.md §1.1）
    "progress_id",
    "added", "skipped", "synced", "updated", "removed", "refreshed", "checked", "abnormal",
    "updated_ids", "removed_ids", "target_ids",
    "total", "processed", "done", "status_label", "tone", "message", "summary_items",
}

_OAUTH_START_FIELDS = {"session_id", "authorize_url", "redirect_uri_prefix"}


def _project_mapping(item: Any, fields: set[str]) -> Any:
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k in fields}


def _project_list(items: Any, fields: set[str], *, limit: int = 0) -> list:
    if not isinstance(items, list):
        return []
    projected = [_project_mapping(item, fields) for item in items]
    return projected[:limit] if limit else projected


def _project_events(items: Any) -> list:
    return _project_list(items, _EVENT_FIELDS, limit=200)


def _project_errors(items: Any) -> list:
    return _project_list(items, _ERROR_FIELDS, limit=50)


def _project_import_job(job: Any) -> dict | None:
    if not isinstance(job, dict):
        return None
    result = _project_mapping(job, _IMPORT_JOB_FIELDS)
    result["errors"] = _project_errors(job.get("errors"))
    result["events"] = _project_events(job.get("events"))
    return result


def _project_mutation(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    result = _project_mapping(data, _MUTATION_FIELDS)
    for key in ("updated_ids", "removed_ids", "target_ids"):
        if key in data:
            result[key] = [str(v) for v in data.get(key) or []]
    if "errors" in data:
        result["errors"] = _project_errors(data.get("errors"))
    if "events" in data:
        result["events"] = _project_events(data.get("events"))
    if "items" in data:
        result["items"] = _project_list(data.get("items"), _ACCOUNT_ITEM_FIELDS)
    return result


def _with_import_job(source: Any) -> Any:
    """把 pool / server 中的 import_job 单独投影，其余字段由调用方白名单裁剪。"""
    if not isinstance(source, dict):
        return source
    return {**source, "import_job": _project_import_job(source.get("import_job"))}


# ---------------------------------------------------------------- 账号列表与变更

@router.get("")
async def list_accounts(
    keyword: str = Query(default=""),
    status: str = Query(default="all"),
    group_id: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    base_url, api_key = await upstream_config(db)
    data = await request_json(
        "GET", base_url, api_key, "/api/accounts",
        params={
            "page": page, "page_size": page_size,
            "keyword": keyword, "status": status, "group_id": group_id,
        },
    )
    if not isinstance(data, dict):
        return data
    return {
        **_project_mapping(data, {"total", "all_total", "page", "page_size"}),
        "items": _project_list(data.get("items"), _ACCOUNT_ITEM_FIELDS),
    }


@router.get("/groups")
async def list_account_groups(db: AsyncSession = Depends(get_db)):
    """上游账号组（用于过滤、导入目标分组与批量绑定）。"""
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, "/api/account-groups")
    return _groups_response(data)


def _groups_response(data: Any) -> dict:
    if not isinstance(data, dict):
        return {"groups": []}
    return {"groups": _project_list(data.get("groups"), _ACCOUNT_GROUP_FIELDS)}


class AccountGroupIn(BaseModel):
    id: str = Field(default="", max_length=120)
    name: str = Field(default="", max_length=200)
    enabled: bool = True
    notes: str = Field(default="", max_length=2000)


@router.post("/groups")
async def save_account_group(
    body: AccountGroupIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    """新建或更新账号组（PicSystem 不代理出口代理配置，proxy 交由上游控制台维护）。"""
    if not (body.id or body.name):
        raise HTTPException(status_code=400, detail="请填写账号组名称")
    base_url, api_key = await upstream_config(db)
    data = await request_json(
        "POST", base_url, api_key, "/api/account-groups",
        json_body={"id": body.id, "name": body.name, "enabled": body.enabled, "notes": body.notes},
    )
    await audit(db, admin_id=admin.id, action="upstream.account_group_save",
                detail=body.id or body.name)
    return _groups_response(data)


@router.delete("/groups/{group_id}")
async def delete_account_group(
    group_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    base_url, api_key = await upstream_config(db)
    data = await request_json("DELETE", base_url, api_key, f"/api/account-groups/{group_id}")
    await audit(db, admin_id=admin.id, action="upstream.account_group_delete", detail=group_id)
    if not isinstance(data, dict):
        return {"groups": []}
    return {
        **_groups_response(data),
        **_project_mutation(data),
    }


class BindGroupIn(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=500)
    group_id: str = Field(default="")


@router.post("/bind-group")
async def bind_accounts_group(
    body: BindGroupIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    """把账号绑定到指定账号组；group_id 传空或 __ungrouped__ 表示移出分组。"""
    base_url, api_key = await upstream_config(db)
    data = await request_json(
        "POST", base_url, api_key, "/api/accounts/group",
        json_body={"account_ids": body.account_ids, "group_id": body.group_id},
    )
    await audit(db, admin_id=admin.id, action="upstream.accounts_bind_group",
                detail=f"{len(body.account_ids)} 个账号 → {body.group_id or '未分组'}")
    if not isinstance(data, dict):
        return _project_mutation(data)
    return {
        **_project_mutation(data),
        **_groups_response(data),
        "group_id": data.get("group_id", ""),
    }


# 导入上限：与上游语义一致，同时避免超大 payload 打爆内存
_MAX_IMPORT_ACCOUNTS = 5000
_MAX_IMPORT_CHARS = 8 * 1024 * 1024
_MAX_ACCOUNT_PAYLOAD_BYTES = 64 * 1024
_MAX_TOKEN_CHARS = 4096


class AddAccountsIn(BaseModel):
    tokens: list[str] = Field(default_factory=list, max_length=_MAX_IMPORT_ACCOUNTS)
    # 结构化账号负载：支持 access_token / refresh_token / id_token / proxy / group_id 等
    # 上游字段（完整备份恢复需要原样透传），至少需包含一个 access token 字段。
    accounts: list[dict] = Field(default_factory=list, max_length=_MAX_IMPORT_ACCOUNTS)
    sync_after_import: bool = True
    restore: bool = False
    return_items: bool = False
    target_group_id: str | None = None


def _normalize_import_payload(body: AddAccountsIn) -> tuple[list[str], list[dict]]:
    tokens: list[str] = []
    total_chars = 0
    for token in body.tokens:
        text = str(token or "").strip()
        if not text:
            continue
        if len(text) > _MAX_TOKEN_CHARS:
            raise HTTPException(status_code=400, detail="单条 Token 长度超过限制")
        tokens.append(text)
        total_chars += len(text)

    accounts: list[dict] = []
    for item in body.accounts:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="账号负载必须是对象数组")
        payload_size = len(json.dumps(item, ensure_ascii=False))
        if payload_size > _MAX_ACCOUNT_PAYLOAD_BYTES:
            raise HTTPException(status_code=400, detail="单个账号负载超过 64KB 上限")
        token = str(item.get("access_token") or item.get("accessToken") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="账号负载缺少 access_token")
        if len(token) > _MAX_TOKEN_CHARS:
            raise HTTPException(status_code=400, detail="单条 Token 长度超过限制")
        accounts.append(item)
        total_chars += payload_size

    total_candidates = len(tokens) + len(accounts)
    if total_candidates == 0:
        raise HTTPException(status_code=400, detail="请至少提供一个 Access Token 或账号负载")
    if total_candidates > _MAX_IMPORT_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"单次导入不超过 {_MAX_IMPORT_ACCOUNTS} 个账号")
    if total_chars > _MAX_IMPORT_CHARS:
        raise HTTPException(status_code=413, detail="导入内容过大，请分批导入")
    return tokens, accounts


@router.post("")
async def add_accounts(body: AddAccountsIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    tokens, accounts = _normalize_import_payload(body)
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "POST", base_url, api_key, "/api/accounts",
        json_body={
            "tokens": tokens,
            "accounts": accounts,
            "sync_after_import": body.sync_after_import,
            "restore": body.restore,
            "return_items": body.return_items,
            "target_group_id": body.target_group_id,
        },
    )
    mode = "完整备份恢复" if body.restore else "导入"
    await audit(db, admin_id=admin.id, action="upstream.accounts_add",
                detail=f"{mode} {len(tokens) + len(accounts)} 个，成功 {result.get('added', 0)}，"
                       f"跳过 {result.get('skipped', 0)}")
    return _project_mutation(result)


class BatchIn(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=500)
    operation: str = Field(pattern="^(enable|disable)$")


@router.post("/batch")
async def batch_update(body: BatchIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    base_url, api_key = await upstream_config(db)
    status = "正常" if body.operation == "enable" else "禁用"
    result = await request_json(
        "POST", base_url, api_key, "/api/accounts/batch-update",
        json_body={"account_ids": body.account_ids, "operation": body.operation, "status": status},
    )
    await audit(db, admin_id=admin.id, action=f"upstream.accounts_{body.operation}",
                detail=f"{len(body.account_ids)} 个账号")
    return _project_mutation(result)


class IdsIn(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=500)


@router.post("/delete")
async def delete_accounts(body: IdsIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "DELETE", base_url, api_key, "/api/accounts", json_body={"account_ids": body.account_ids}
    )
    await audit(db, admin_id=admin.id, action="upstream.accounts_delete",
                detail=f"{len(body.account_ids)} 个账号")
    return _project_mutation(result)


class SyncIn(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=500)


@router.post("/sync")
async def sync_accounts(body: SyncIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """同步远程元数据与图片额度；空列表 = 全部账号。"""
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "POST", base_url, api_key, "/api/accounts/sync", json_body={"account_ids": body.account_ids}
    )
    await audit(db, admin_id=admin.id, action="upstream.accounts_sync",
                detail=f"{len(body.account_ids) or '全部'} 个账号")
    return _project_mutation(result)


class CleanupIn(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=2000)
    remove: bool = False


@router.post("/import-cleanup")
async def cleanup_imported(
    body: CleanupIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    """预览/移除「本次导入且已确认鉴权失效」的账号（remove=False 仅预览）。"""
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "POST", base_url, api_key, "/api/accounts/import-cleanup",
        json_body={"account_ids": body.account_ids, "remove": body.remove},
    )
    if body.remove:
        await audit(db, admin_id=admin.id, action="upstream.accounts_import_cleanup",
                    detail=f"移除 {len(body.account_ids)} 个候选中的确认失效账号")
    if not isinstance(result, dict):
        return result
    return {**_project_mutation(result), "abnormal": int(result.get("abnormal") or 0)}


@router.get("/operations/{progress_id}")
async def operation_progress(progress_id: str, db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, f"/api/accounts/operations/{progress_id}")
    if not isinstance(data, dict):
        return data
    return {
        **_project_mapping(
            data,
            {"done", "processed", "total", "stage", "stage_label", "error",
             "status_label", "tone", "message", "summary_items"},
        ),
        "events": _project_events(data.get("events")),
        "result": _project_mutation(data.get("result")) if isinstance(data.get("result"), dict) else data.get("result"),
    }


# ---------------------------------------------------------------- OAuth 登录导入

class OAuthStartIn(BaseModel):
    email_hint: str = Field(default="", max_length=320)


@router.post("/oauth/start")
async def oauth_start(body: OAuthStartIn, db: AsyncSession = Depends(get_db)):
    """登记一次 PKCE 会话并返回可让管理员浏览器打开的授权地址。"""
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "POST", base_url, api_key, "/api/accounts/oauth/start",
        json_body={"email_hint": body.email_hint},
    )
    return _project_mapping(result, _OAUTH_START_FIELDS)


class OAuthFinishIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    callback: str = Field(min_length=1, max_length=16384)
    target_group_id: str | None = None


@router.post("/oauth/finish")
async def oauth_finish(
    body: OAuthFinishIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    """用管理员从浏览器抓回的 callback URL / code 换取 RT 并落盘。"""
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "POST", base_url, api_key, "/api/accounts/oauth/finish",
        json_body={
            "session_id": body.session_id,
            "callback": body.callback,
            "target_group_id": body.target_group_id,
        },
    )
    await audit(db, admin_id=admin.id, action="upstream.accounts_oauth_import", detail="OAuth 登录导入 1 个账号")
    return _project_mutation(result)


# ---------------------------------------------------------------- 远程 CPA

class CPAPoolIn(BaseModel):
    name: str = Field(default="", max_length=120)
    base_url: str = Field(max_length=512)
    secret_key: str = Field(max_length=1024)


@router.get("/cpa/pools")
async def list_cpa_pools(db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, "/api/cpa/pools")
    pools = _project_list((data or {}).get("pools") if isinstance(data, dict) else None, _CPA_POOL_FIELDS)
    return {"pools": [_with_import_job(p) for p in pools]}


@router.post("/cpa/pools")
async def create_cpa_pool(
    body: CPAPoolIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    base_url, api_key = await upstream_config(db)
    data = await request_json(
        "POST", base_url, api_key, "/api/cpa/pools",
        json_body={"name": body.name, "base_url": body.base_url, "secret_key": body.secret_key},
    )
    await audit(db, admin_id=admin.id, action="upstream.cpa_pool_create", detail=body.name or body.base_url)
    return _pool_response(data)


class CPAPoolUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=512)
    secret_key: str | None = Field(default=None, max_length=1024)


@router.post("/cpa/pools/{pool_id}")
async def update_cpa_pool(
    pool_id: str, body: CPAPoolUpdateIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    base_url, api_key = await upstream_config(db)
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    data = await request_json("POST", base_url, api_key, f"/api/cpa/pools/{pool_id}", json_body=payload)
    await audit(db, admin_id=admin.id, action="upstream.cpa_pool_update", detail=pool_id)
    return _pool_response(data)


@router.delete("/cpa/pools/{pool_id}")
async def delete_cpa_pool(pool_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("DELETE", base_url, api_key, f"/api/cpa/pools/{pool_id}")
    await audit(db, admin_id=admin.id, action="upstream.cpa_pool_delete", detail=pool_id)
    return _pool_response(data)


def _pool_response(data: Any) -> dict:
    if not isinstance(data, dict):
        return {"pools": []}
    pools = _project_list(data.get("pools"), _CPA_POOL_FIELDS)
    return {
        "pool": _with_import_job(_project_mapping(data.get("pool"), _CPA_POOL_FIELDS)),
        "pools": [_with_import_job(p) for p in pools],
    }


@router.get("/cpa/pools/{pool_id}/files")
async def cpa_pool_files(pool_id: str, db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, f"/api/cpa/pools/{pool_id}/files")
    files = _project_list((data or {}).get("files") if isinstance(data, dict) else None, _CPA_FILE_FIELDS)
    return {"pool_id": pool_id, "files": files}


class CPAImportIn(BaseModel):
    names: list[str] = Field(min_length=1, max_length=500)
    target_group_id: str | None = None


@router.post("/cpa/pools/{pool_id}/import")
async def cpa_pool_import(
    pool_id: str, body: CPAImportIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    base_url, api_key = await upstream_config(db)
    data = await request_json(
        "POST", base_url, api_key, f"/api/cpa/pools/{pool_id}/import",
        json_body={"names": body.names, "target_group_id": body.target_group_id},
    )
    await audit(db, admin_id=admin.id, action="upstream.cpa_pool_import", detail=f"{len(body.names)} 个文件")
    return {"import_job": _project_import_job((data or {}).get("import_job") if isinstance(data, dict) else None)}


@router.get("/cpa/pools/{pool_id}/import")
async def cpa_pool_import_progress(pool_id: str, db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, f"/api/cpa/pools/{pool_id}/import")
    return {"import_job": _project_import_job((data or {}).get("import_job") if isinstance(data, dict) else None)}


# ---------------------------------------------------------------- 远程 Sub2API

class Sub2APIServerIn(BaseModel):
    name: str = Field(default="", max_length=120)
    base_url: str = Field(max_length=512)
    email: str = Field(default="", max_length=320)
    password: str = Field(default="", max_length=1024)
    api_key: str = Field(default="", max_length=2048)
    group_id: str = Field(default="", max_length=120)


@router.get("/sub2api/servers")
async def list_sub2api_servers(db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, "/api/sub2api/servers")
    servers = _project_list((data or {}).get("servers") if isinstance(data, dict) else None, _SUB2API_SERVER_FIELDS)
    return {"servers": [_with_import_job(s) for s in servers]}


@router.post("/sub2api/servers")
async def create_sub2api_server(
    body: Sub2APIServerIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    if not (body.email and body.password) and not body.api_key:
        raise HTTPException(status_code=400, detail="请填写邮箱+密码，或 API Key 之一")
    base_url, api_key = await upstream_config(db)
    data = await request_json("POST", base_url, api_key, "/api/sub2api/servers", json_body=body.model_dump())
    await audit(db, admin_id=admin.id, action="upstream.sub2api_server_create", detail=body.name or body.base_url)
    return _server_response(data)


class Sub2APIServerUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=512)
    email: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=1024)
    api_key: str | None = Field(default=None, max_length=2048)
    group_id: str | None = Field(default=None, max_length=120)


@router.post("/sub2api/servers/{server_id}")
async def update_sub2api_server(
    server_id: str, body: Sub2APIServerUpdateIn,
    db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin),
):
    base_url, api_key = await upstream_config(db)
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    data = await request_json("POST", base_url, api_key, f"/api/sub2api/servers/{server_id}", json_body=payload)
    await audit(db, admin_id=admin.id, action="upstream.sub2api_server_update", detail=server_id)
    return _server_response(data)


@router.delete("/sub2api/servers/{server_id}")
async def delete_sub2api_server(
    server_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    base_url, api_key = await upstream_config(db)
    data = await request_json("DELETE", base_url, api_key, f"/api/sub2api/servers/{server_id}")
    await audit(db, admin_id=admin.id, action="upstream.sub2api_server_delete", detail=server_id)
    return _server_response(data)


def _server_response(data: Any) -> dict:
    if not isinstance(data, dict):
        return {"servers": []}
    servers = _project_list(data.get("servers"), _SUB2API_SERVER_FIELDS)
    return {
        "server": _with_import_job(_project_mapping(data.get("server"), _SUB2API_SERVER_FIELDS)),
        "servers": [_with_import_job(s) for s in servers],
    }


@router.get("/sub2api/servers/{server_id}/groups")
async def sub2api_server_groups(server_id: str, db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, f"/api/sub2api/servers/{server_id}/groups")
    groups = _project_list((data or {}).get("groups") if isinstance(data, dict) else None, _SUB2API_GROUP_FIELDS)
    return {"server_id": server_id, "groups": groups}


@router.get("/sub2api/servers/{server_id}/accounts")
async def sub2api_server_accounts(
    server_id: str,
    group_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    base_url, api_key = await upstream_config(db)
    params = {"group_id": group_id} if group_id is not None else None
    data = await request_json(
        "GET", base_url, api_key, f"/api/sub2api/servers/{server_id}/accounts", params=params
    )
    accounts = _project_list(
        (data or {}).get("accounts") if isinstance(data, dict) else None, _SUB2API_ACCOUNT_FIELDS
    )
    return {"server_id": server_id, "accounts": accounts}


class Sub2APIGroupBinding(BaseModel):
    remote_group_id: str = Field(default="", max_length=120)
    name: str = Field(default="", max_length=200)
    account_ids: list[str] = Field(default_factory=list, max_length=2000)


class Sub2APIImportIn(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=2000)
    group_bindings: list[Sub2APIGroupBinding] = Field(default_factory=list, max_length=200)
    create_account_groups: bool = True
    target_group_id: str | None = None


@router.post("/sub2api/servers/{server_id}/import")
async def sub2api_server_import(
    server_id: str, body: Sub2APIImportIn,
    db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin),
):
    base_url, api_key = await upstream_config(db)
    data = await request_json(
        "POST", base_url, api_key, f"/api/sub2api/servers/{server_id}/import",
        json_body={
            "account_ids": body.account_ids,
            "group_bindings": [b.model_dump() for b in body.group_bindings],
            "create_account_groups": body.create_account_groups,
            "target_group_id": body.target_group_id,
        },
    )
    await audit(db, admin_id=admin.id, action="upstream.sub2api_server_import",
                detail=f"{len(body.account_ids)} 个账号")
    return {"import_job": _project_import_job((data or {}).get("import_job") if isinstance(data, dict) else None)}


@router.get("/sub2api/servers/{server_id}/import")
async def sub2api_server_import_progress(server_id: str, db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    data = await request_json("GET", base_url, api_key, f"/api/sub2api/servers/{server_id}/import")
    return {"import_job": _project_import_job((data or {}).get("import_job") if isinstance(data, dict) else None)}
