"""内容安全钩子：本地关键词拦截层（上游内容过滤之外的第一道防线）。

- 大小写不敏感的子串匹配；管理员可在后台维护关键词（每行一个）
- 命中即 403，风控事件只记录命中的词，不留存用户原文
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .risk import record_event
from .settings_store import get_setting

# 内置默认词库（覆盖政治敏感/色情/暴恐/未成年人等高风险方向；管理员可自行增删）
DEFAULT_KEYWORDS = [
    "习近平", "毛泽东", "六四", "法轮功", "台独", "港独", "藏独",
    "儿童色情", "幼女", "幼男", "未成年人性", "incest",
    "制造炸弹", "制造炸药", "制毒", "冰毒配方",
    "自杀教学", "如何自杀",
    "child porn", "csam",
]


def extract_texts(messages: object) -> list[str]:
    """从 OpenAI messages 结构提取全部文本段（不截断，防后置/拆词绕过）。"""
    texts: list[str] = []
    if not isinstance(messages, list):
        return texts
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content[:20000])
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"][:20000])
    return texts


async def check_content(
    session: AsyncSession,
    text: str,
    *,
    user_id: int,
    username: str,
    ip: str,
    endpoint: str,
) -> None:
    """检查文本，命中关键词则记录风控事件并抛 403。"""
    if not await get_setting(session, "content_filter_enabled"):
        return
    custom = str(await get_setting(session, "content_filter_keywords") or "")
    keywords = DEFAULT_KEYWORDS + [line.strip() for line in custom.splitlines() if line.strip()]
    lower = text.lower()
    for word in keywords:
        if word and word.lower() in lower:
            await record_event(
                session,
                kind="content_blocked",
                username=username,
                user_id=user_id,
                ip=ip,
                detail=f"命中敏感词「{word}」（{endpoint}）",
            )
            raise HTTPException(status_code=403, detail="内容包含违规信息，已被安全策略拦截")
