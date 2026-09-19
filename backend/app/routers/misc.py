"""模型目录（上游 /v1/models 代理，60 秒缓存）与我的调用记录。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import get_current_user
from ..models import UsageLog, User
from ..services.upstream import UpstreamError, request_json, upstream_config

router = APIRouter(prefix="/api", tags=["misc"])

FALLBACK_MODELS = {
    "object": "list",
    "data": [
        {"id": "auto", "object": "model", "owned_by": "fallback"},
        {"id": "gpt-image-2", "object": "model", "owned_by": "fallback"},
    ],
}

_models_cache: dict = {"ts": 0.0, "data": None}


@router.get("/models")
async def list_models(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    now = time.monotonic()
    if _models_cache["data"] is not None and now - _models_cache["ts"] < 60:
        return _models_cache["data"]
    try:
        base_url, api_key = await upstream_config(db)
        data = await request_json("GET", base_url, api_key, "/v1/models")
        _models_cache.update(ts=now, data=data)
        return data
    except UpstreamError:
        if _models_cache["data"] is not None:
            return _models_cache["data"]
        return FALLBACK_MODELS


@router.get("/me/logs")
async def my_logs(
    feature: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(UsageLog).where(UsageLog.user_id == user.id)
    count_q = select(func.count(UsageLog.id)).where(UsageLog.user_id == user.id)
    if feature:
        q = q.where(UsageLog.feature == feature)
        count_q = count_q.where(UsageLog.feature == feature)
    total = await db.scalar(count_q) or 0
    result = await db.execute(
        q.order_by(UsageLog.created_at.desc()).offset((page - 1) * size).limit(size)
    )
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "feature": r.feature,
                "model": r.model,
                "endpoint": r.endpoint,
                "status": r.status,
                "error": r.error,
                "latency_ms": r.latency_ms,
                "units": r.units,
                "created_at": r.created_at,
            }
            for r in result.scalars()
        ],
    }
