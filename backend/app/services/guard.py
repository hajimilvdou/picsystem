"""功能开关 + 上游配置守卫。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FEATURE_LABELS
from .settings_store import feature_flags
from .upstream import upstream_config


async def require_feature(session: AsyncSession, feature: str) -> tuple[str, str]:
    """校验功能开关，返回 (upstream_base_url, api_key)。"""
    flags = await feature_flags(session)
    if not flags.get(feature, False):
        raise HTTPException(status_code=403, detail=f"{FEATURE_LABELS.get(feature, feature)}功能已被管理员关闭")
    return await upstream_config(session)
