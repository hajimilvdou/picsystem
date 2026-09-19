"""登录设备会话与设备指纹。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UserSession

# 距上次活跃超过该间隔才写库，避免每请求一次 UPDATE
_TOUCH_INTERVAL = timedelta(minutes=5)


def device_label_from_ua(ua: str) -> str:
    ua_l = ua.lower()
    if "windows" in ua_l:
        os_name = "Windows"
    elif "iphone" in ua_l or "ipad" in ua_l:
        os_name = "iPhone/iPad"
    elif "mac os" in ua_l or "macintosh" in ua_l:
        os_name = "macOS"
    elif "android" in ua_l:
        os_name = "Android"
    elif "linux" in ua_l:
        os_name = "Linux"
    else:
        os_name = "未知系统"
    if "edg/" in ua_l or "edge/" in ua_l:
        browser = "Edge"
    elif "firefox/" in ua_l:
        browser = "Firefox"
    elif "chrome/" in ua_l:
        browser = "Chrome"
    elif "safari/" in ua_l:
        browser = "Safari"
    else:
        browser = "浏览器"
    return f"{os_name} · {browser}"


async def create_session(db: AsyncSession, *, user_id: int, ip: str, user_agent: str) -> UserSession:
    row = UserSession(
        id=uuid.uuid4().hex,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent[:300],
        device_label=device_label_from_ua(user_agent),
    )
    db.add(row)
    await db.flush()
    return row


async def touch_session(db: AsyncSession, session: UserSession) -> None:
    """按节流间隔更新 last_seen_at。

    用独立会话写库，绝不提交调用方请求会话中未提交的状态。
    """
    now = datetime.now(timezone.utc)
    last = session.last_seen_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or now - last >= _TOUCH_INTERVAL:
        from ..database import SessionLocal

        async with SessionLocal() as write_db:
            await write_db.execute(
                update(UserSession).where(UserSession.id == session.id).values(last_seen_at=now)
            )
            await write_db.commit()
        session.last_seen_at = now


async def list_sessions(db: AsyncSession, user_id: int, *, active_within: timedelta) -> list[UserSession]:
    cutoff = datetime.now(timezone.utc) - active_within
    result = await db.execute(
        select(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked.is_(False),
            UserSession.last_seen_at >= cutoff,  # JWT 过期后的“幽灵设备”不再展示
        )
        .order_by(UserSession.last_seen_at.desc(), UserSession.created_at.desc())
        .limit(50)
    )
    return list(result.scalars())


async def revoke_session(db: AsyncSession, user_id: int, session_id: str) -> bool:
    result = await db.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.user_id == user_id)
        .values(revoked=True)
    )
    await db.commit()
    return result.rowcount == 1


async def revoke_all_sessions(db: AsyncSession, user_id: int, except_id: str | None = None) -> None:
    stmt = update(UserSession).where(UserSession.user_id == user_id).values(revoked=True)
    if except_id:
        stmt = stmt.where(UserSession.id != except_id)
    await db.execute(stmt)


def fingerprint_hash(raw_fp: str) -> str:
    """设备指纹哈希入库：不掺 IP（换 IP 不应绕过风控），加盐防彩虹表。"""
    from ..config import settings

    return hashlib.sha256(f"picsystem-fp|{settings.jwt_secret[:16]}|{raw_fp}".encode()).hexdigest()[:48]
