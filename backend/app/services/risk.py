"""用户风控：登录防爆破锁定、注册防刷、风控事件记录。

设计说明：
- 登录锁定按【用户名+IP】：既防爆破，又避免攻击者锁定其他网络位置的正常
  用户；管理员可在风控中心按组合解锁。
- IP 可信度由本项目 nginx 保证（默认 XFF_MODE=$remote_addr 覆盖客户端
  伪造的 X-Forwarded-For）；外部反代场景的取舍见 README「域名反代」一节。
- 注册/限流按 IP 维度：注册仍需邀请码，残留风险可控。
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RiskEvent


async def record_event(
    session: AsyncSession,
    *,
    kind: str,
    username: str = "",
    user_id: int | None = None,
    ip: str = "",
    detail: str = "",
) -> None:
    session.add(
        RiskEvent(
            kind=kind[:32],
            username=username[:64],
            user_id=user_id,
            ip=ip[:64],
            detail=detail[:500],
        )
    )
    await session.commit()


class LoginProtector:
    """内存版登录防爆破：按【用户名+IP】滑动窗口计数，超阈值临时锁定该组合。

    采用"用户名+IP"而非纯用户名：攻击者从单一来源无法锁定其他网络位置的
    正常用户（防用户名维度 DoS）；配合 nginx 防伪造的 XFF（见 compose
    XFF_MODE 说明），换 IP 绕过锁定的成本与直接爆破相同。
    """

    def __init__(self) -> None:
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(username: str, ip: str) -> str:
        return f"{username.lower()}|{ip}"

    async def locked_remaining(self, username: str, ip: str) -> int:
        """返回剩余锁定秒数，未锁定返回 0。"""
        now = time.monotonic()
        async with self._lock:
            until = self._locked_until.get(self._key(username, ip))
            if until is None:
                return 0
            if until <= now:
                self._locked_until.pop(self._key(username, ip), None)
                return 0
            return int(until - now) + 1

    async def register_failure(
        self,
        username: str,
        ip: str,
        *,
        max_failures: int,
        window_seconds: int = 600,
        lock_seconds: int = 900,
    ) -> int:
        """记录一次失败；若因此触发锁定，返回锁定时长（秒），否则返回 0。"""
        now = time.monotonic()
        key = self._key(username, ip)
        async with self._lock:
            q = self._fails[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            q.append(now)
            if len(q) >= max_failures:
                self._locked_until[key] = now + lock_seconds
                q.clear()
                return lock_seconds
            if len(self._fails) > 50000:
                self._fails.clear()
            return 0

    async def clear(self, username: str, ip: str) -> None:
        async with self._lock:
            key = self._key(username, ip)
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)

    async def list_locks(self) -> list[dict]:
        now = time.monotonic()
        async with self._lock:
            expired = [k for k, t in self._locked_until.items() if t <= now]
            for k in expired:
                self._locked_until.pop(k, None)
            return [
                {"username": k.rsplit("|", 1)[0], "ip": k.rsplit("|", 1)[1], "remaining_seconds": int(t - now)}
                for k, t in sorted(self._locked_until.items())
            ]


login_protector = LoginProtector()


async def registrations_today(session: AsyncSession, ip: str) -> int:
    """该 IP 今日注册成功数（按 users 表统计，不受日志保留策略影响）。"""
    from ..models import User

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return await session.scalar(
        select(func.count(User.id)).where(User.reg_ip == ip, User.created_at >= day_start)
    ) or 0
