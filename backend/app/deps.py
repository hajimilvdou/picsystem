"""认证依赖、限流、在途请求闸门。"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import get_db
from .models import User, UserSession, UserStatus
from .security import decode_session_token
from .services.sessions import touch_session


def client_ip(request: Request) -> str:
    # 部署在本项目 nginx 之后：X-Forwarded-For 由它追加，链首即最原始客户端
    # （外部域名反代 → 本项目 nginx → api 时同样成立）
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()[:64]
    return (request.client.host if request.client else "")[:64]


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
    try:
        user_id = int(payload["sub"])
        token_version = int(payload.get("tv", -1))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录") from None
    user = await db.get(User, user_id)
    if user is None or user.status == UserStatus.DISABLED.value:
        raise HTTPException(status_code=401, detail="账号不可用")
    if user.token_version != token_version:
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
    # 设备会话校验：每次登录一条记录，可单独吊销；无 sid 的旧会话一律要求重登
    sid = str(payload.get("sid") or "")
    if not sid:
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
    session = await db.get(UserSession, sid)
    if session is None or session.revoked or session.user_id != user.id:
        raise HTTPException(status_code=401, detail="该设备登录已被注销，请重新登录")
    # 待审核账号：仅可访问 /api/auth/me 与 /api/auth/logout（等待页轮询用）
    if user.status == UserStatus.PENDING.value:
        if request.url.path not in ("/api/auth/me", "/api/auth/logout"):
            raise HTTPException(status_code=403, detail="账号正在审核中，请耐心等待管理员通过")
    request.state.user_session = session
    await touch_session(db, session)
    # 协议版本强制：/api/auth/* 之外的业务端点必须先同意当前版本协议
    if not request.url.path.startswith("/api/auth/"):
        from .services.settings_store import get_setting

        required = int(await get_setting(db, "agreement_version") or 1)
        if user.agreement_version < required:
            raise HTTPException(status_code=428, detail="请先阅读并同意用户协议")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---- 简易内存限流（滑动窗口）----

class SlidingRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            # 容量控制：逐出最旧的 10% 键而非整体清空，防伪造键名冲垮限流
            if len(self._hits) > 20000:
                for stale in list(self._hits)[:2000]:
                    if stale != key:
                        self._hits.pop(stale, None)
            return True


rate_limiter = SlidingRateLimiter()


def rate_limit(scope: str, limit: int, window_seconds: int = 60):
    async def dependency(request: Request) -> None:
        key = f"{scope}:{client_ip(request)}"
        if not await rate_limiter.hit(key, limit, window_seconds):
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    return dependency


def user_rate_limit(scope: str):
    """按用户限流（额度见风控设置），超限时写入风控事件。"""

    async def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        from .services.risk import record_event
        from .services.settings_store import get_setting

        limit = int(await get_setting(db, "user_rate_limit_per_minute") or 20)
        if not await rate_limiter.hit(f"{scope}:{user.id}", limit, 60):
            await record_event(
                db,
                kind="rate_limit",
                username=user.username,
                user_id=user.id,
                ip=client_ip(request),
                detail=f"功能 {scope} 超过每分钟 {limit} 次",
            )
            raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    return dependency


# ---- 在途请求闸门（防并发滥用 & 供监控展示）----

async def inflight_limit(db: AsyncSession, user: User) -> int:
    """单用户并发上限：个人覆盖优先，否则全局默认。"""
    from .services.settings_store import get_setting

    if user.max_inflight is not None:
        return max(user.max_inflight, 1)
    return max(int(await get_setting(db, "max_inflight_default") or 2), 1)


class InflightGuard:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, key: str, limit: int) -> AsyncIterator[None]:
        async with self._lock:
            if self._counts[key] >= limit:
                raise HTTPException(status_code=429, detail="同时进行的请求过多，请等待之前的请求完成")
            self._counts[key] += 1
        try:
            yield
        finally:
            async with self._lock:
                self._counts[key] -= 1
                if self._counts[key] <= 0:
                    self._counts.pop(key, None)

    async def total(self) -> int:
        async with self._lock:
            return sum(self._counts.values())


inflight = InflightGuard()
