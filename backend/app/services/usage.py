"""调用日志与管理员审计。"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog, UsageLog


async def log_usage(
    session: AsyncSession,
    *,
    user_id: int,
    feature: str,
    endpoint: str,
    status: str,
    model: str = "",
    api_key_id: int | None = None,
    error: str = "",
    latency_ms: int = 0,
    units: int = 1,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    ip: str = "",
) -> None:
    session.add(
        UsageLog(
            user_id=user_id,
            api_key_id=api_key_id,
            feature=feature,
            model=model[:100],
            endpoint=endpoint[:64],
            status=status,
            error=error[:500],
            latency_ms=latency_ms,
            units=units,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            ip=ip[:64],
        )
    )
    await session.commit()


async def audit(session: AsyncSession, *, admin_id: int, action: str, target: str = "", detail: str = "") -> None:
    session.add(AuditLog(admin_id=admin_id, action=action[:64], target=target[:128], detail=detail[:1000]))
    await session.commit()
