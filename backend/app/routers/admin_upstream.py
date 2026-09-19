"""管理端：上游 chatgpt2api 账号管理（路线 C——密钥不出服务端，上游零端口暴露）。

代理上游管理 API（/api/accounts*）。该 API 属上游内部契约（区别于稳定的
/v1 兼容契约），适配与降级预案见 docs/UPSTREAM-ADMIN-API.md。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import User
from ..services.upstream import request_json, upstream_config
from ..services.usage import audit

router = APIRouter(prefix="/api/admin/upstream-accounts", tags=["admin-upstream"], dependencies=[Depends(require_admin)])

# 账号投影白名单：只把这些字段透传到前端（防上游版本新增敏感字段穿透，见 docs/UPSTREAM-ADMIN-API.md §4）
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


def _sanitize(data):
    """对响应中的 items[] 按白名单投影；其余顶层字段原样保留。"""
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = {
            **data,
            "items": [
                {k: v for k, v in item.items() if k in _ACCOUNT_ITEM_FIELDS}
                if isinstance(item, dict) else item
                for item in data["items"]
            ],
        }
    return data


@router.get("")
async def list_accounts(
    keyword: str = Query(default=""),
    status: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    base_url, api_key = await upstream_config(db)
    data = await request_json(
        "GET", base_url, api_key, "/api/accounts",
        params={"page": page, "page_size": page_size, "keyword": keyword, "status": status},
    )
    return _sanitize(data)


class AddAccountsIn(BaseModel):
    tokens: list[str] = Field(default_factory=list, max_length=50)
    sync_after_import: bool = True


@router.post("")
async def add_accounts(body: AddAccountsIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    tokens = [t.strip() for t in body.tokens if t.strip()]
    if not tokens:
        return {"added": 0, "skipped": 0, "errors": [], "items": []}
    if any(len(t) > 4096 for t in tokens):
        raise HTTPException(status_code=400, detail="单条 Token 长度超过限制")
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "POST", base_url, api_key, "/api/accounts",
        json_body={"tokens": tokens, "sync_after_import": body.sync_after_import, "return_items": True},
    )
    await audit(db, admin_id=admin.id, action="upstream.accounts_add",
                detail=f"添加 {len(tokens)} 个，成功 {result.get('added', 0)}，跳过 {result.get('skipped', 0)}")
    return _sanitize(result)


class BatchIn(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=200)
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
    return result


class IdsIn(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=200)


@router.post("/delete")
async def delete_accounts(body: IdsIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    base_url, api_key = await upstream_config(db)
    result = await request_json(
        "DELETE", base_url, api_key, "/api/accounts", json_body={"account_ids": body.account_ids}
    )
    await audit(db, admin_id=admin.id, action="upstream.accounts_delete",
                detail=f"{len(body.account_ids)} 个账号")
    return result


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
    return result


@router.get("/operations/{progress_id}")
async def operation_progress(progress_id: str, db: AsyncSession = Depends(get_db)):
    base_url, api_key = await upstream_config(db)
    return await request_json("GET", base_url, api_key, f"/api/accounts/operations/{progress_id}")
