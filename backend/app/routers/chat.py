"""对话：会话管理 + SSE 流式转发。"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..database import SessionLocal, get_db
from ..deps import client_ip, get_current_user, inflight, inflight_limit, user_rate_limit
from ..models import Conversation, Message, User
from ..schemas import ChatSendIn, ConversationRenameIn
from ..services.content_guard import check_content
from ..services.guard import require_feature
from ..services.quota import consume, refund
from ..services.text_filter import WritingMarkerFilter
from ..services.upstream import UpstreamError, stream_chat
from ..services.usage import log_usage

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _get_own_conversation(db: AsyncSession, user_id: int, conv_id: int) -> Conversation:
    conv = await db.get(Conversation, conv_id, options=[selectinload(Conversation.messages)])
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conv


@router.get("/conversations")
async def list_conversations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(200)
    )
    return {
        "items": [
            {"id": c.id, "title": c.title, "model": c.model, "updated_at": c.updated_at}
            for c in result.scalars()
        ]
    }


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    conv = await _get_own_conversation(db, user.id, conv_id)
    return {
        "id": conv.id,
        "title": conv.title,
        "model": conv.model,
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "meta": m.meta, "created_at": m.created_at}
            for m in conv.messages
        ],
    }


@router.patch("/conversations/{conv_id}")
async def rename_conversation(
    conv_id: int, body: ConversationRenameIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    conv = await _get_own_conversation(db, user.id, conv_id)
    conv.title = body.title
    await db.commit()
    return {"ok": True}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    conv = await _get_own_conversation(db, user.id, conv_id)
    await db.delete(conv)
    await db.commit()
    return {"ok": True}


@router.post("/completions", dependencies=[Depends(user_rate_limit("chat"))])
async def send_message(
    body: ChatSendIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base_url, api_key = await require_feature(db, "chat")
    await check_content(
        db, body.message, user_id=user.id, username=user.username,
        ip=client_ip(request), endpoint="/api/chat/completions",
    )

    # 先解析会话归属（404 不扣额度），再扣额度，最后落库
    if body.conversation_id is not None:
        conv = await _get_own_conversation(db, user.id, body.conversation_id)
        prior = [
            {"role": m.role, "content": m.content}
            for m in conv.messages[-settings.chat_history_limit :]
            if m.role in ("user", "assistant", "system") and m.content
        ]
    else:
        conv = Conversation(user_id=user.id, title=body.message[:24] or "新对话", model=body.model)
        prior = []

    ok, take_temp, take_perm = await consume(db, user.id, "chat", 1)
    if not ok:
        raise HTTPException(status_code=403, detail="对话次数额度不足，请联系管理员")

    # 先抢并发槽再开流：并发超限时 429 且额度已退
    limit = await inflight_limit(db, user)
    cm = inflight.acquire(f"user:{user.id}", limit)
    try:
        await cm.__aenter__()
    except HTTPException:
        await refund(db, user.id, "chat", 1, take_temp, take_perm)
        raise

    if conv.id is None:
        db.add(conv)
        await db.flush()

    history = prior + [{"role": "user", "content": body.message}]

    db.add(Message(conversation_id=conv.id, role="user", content=body.message))
    await db.execute(update(Conversation).where(Conversation.id == conv.id).values(updated_at=func.now()))
    await db.commit()

    conv_id, model, ip = conv.id, body.model, client_ip(request)
    user_id = user.id

    async def event_stream():
        parts: list[str] = []
        usage: dict = {}
        status, error = "success", ""
        started = time.perf_counter()
        marker_filter = WritingMarkerFilter()  # 过滤上游泄露的 :::writing{...} 标记（仅网页显示层）
        yield _sse("meta", {"conversation_id": conv_id})
        try:
            async for kind, data in stream_chat(
                base_url, api_key, {"model": model, "messages": history, "stream": True}
            ):
                if kind == "delta":
                    clean = marker_filter.feed(str(data))
                    if clean:
                        parts.append(clean)
                        yield _sse("delta", {"text": clean})
                elif kind == "usage":
                    usage = data if isinstance(data, dict) else {}
            tail = marker_filter.flush()
            if tail:
                parts.append(tail)
                yield _sse("delta", {"text": tail})
        except UpstreamError as exc:
            status, error = "failed", exc.message
            yield _sse("error", {"detail": exc.message})
        except (asyncio.CancelledError, GeneratorExit):
            status, error = "failed", "客户端中断"
            raise
        finally:
            await cm.__aexit__(None, None, None)
            latency = int((time.perf_counter() - started) * 1000)
            content = "".join(parts)
            async with SessionLocal() as session:
                if content:
                    session.add(Message(conversation_id=conv_id, role="assistant", content=content))
                    await session.execute(
                        update(Conversation).where(Conversation.id == conv_id).values(updated_at=func.now())
                    )
                    await session.commit()
                if status != "success":
                    await refund(session, user_id, "chat", 1, take_temp, take_perm)
                await log_usage(
                    session,
                    user_id=user_id,
                    feature="chat",
                    endpoint="/api/chat/completions",
                    model=model,
                    status=status,
                    error=error,
                    latency_ms=latency,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    ip=ip,
                )
        yield _sse("done", {"conversation_id": conv_id})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
