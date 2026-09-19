"""联网搜索：转发上游 /v1/search。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import client_ip, get_current_user, inflight, inflight_limit, user_rate_limit
from ..models import User
from ..schemas import SearchIn
from ..services.content_guard import check_content
from ..services.guard import require_feature
from ..services.quota import consume, refund
from ..services.upstream import UpstreamError, request_json
from ..services.usage import log_usage

router = APIRouter(prefix="/api/search", tags=["search"])


@router.post("", dependencies=[Depends(user_rate_limit("search"))])
async def search(
    body: SearchIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base_url, api_key = await require_feature(db, "search")
    await check_content(
        db, body.prompt, user_id=user.id, username=user.username,
        ip=client_ip(request), endpoint="/api/search",
    )
    ok, take_temp, take_perm = await consume(db, user.id, "search", 1)
    if not ok:
        raise HTTPException(status_code=403, detail="搜索次数额度不足，请联系管理员")

    started = time.perf_counter()
    status, error = "success", ""
    result: dict = {}
    try:
        async with inflight.acquire(f"user:{user.id}", await inflight_limit(db, user)):
            result = await request_json("POST", base_url, api_key, "/v1/search", json_body={"prompt": body.prompt})
    except HTTPException:
        await refund(db, user.id, "search", 1, take_temp, take_perm)
        raise
    except UpstreamError as exc:
        status, error = "failed", exc.message
    latency = int((time.perf_counter() - started) * 1000)

    if status != "success":
        await refund(db, user.id, "search", 1, take_temp, take_perm)
    await log_usage(
        db,
        user_id=user.id,
        feature="search",
        endpoint="/api/search",
        model="web-search",
        status=status,
        error=error,
        latency_ms=latency,
        ip=client_ip(request),
    )
    if status != "success":
        raise HTTPException(status_code=502, detail=error)
    return result
