"""把上游图片结果（b64 或 url）持久化为本地 StoredFile。"""
from __future__ import annotations

import base64
import binascii

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import StoredFile
from .storage import delete_file, ensure_storage_capacity, save_bytes, StorageFullError
from .upstream import download_upstream_file


async def persist_image_results(
    session: AsyncSession,
    *,
    user,
    base_url: str,
    api_key: str,
    data: dict,
    prompt: str,
) -> list[StoredFile]:
    """解析上游 /v1/images/* 响应，下载/解码并保存，返回已存文件。

    若中途触发存储配额（StorageFullError），回滚本批次未提交的行并清理
    已落盘文件，避免"响应失败但图库多出文件"的状态不一致。
    """
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    saved: list[StoredFile] = []
    try:
        for item in items:
            if not isinstance(item, dict):
                continue
            content: bytes | None = None
            mime = "image/png"
            if item.get("b64_json"):
                try:
                    content = base64.b64decode(str(item["b64_json"]), validate=False)
                except (binascii.Error, ValueError):
                    continue
            elif item.get("url"):
                try:
                    content, mime = await download_upstream_file(base_url, api_key, str(item["url"]))
                except Exception:
                    continue
            if not content:
                continue
            await ensure_storage_capacity(session, user, len(content))
            rel, final_name = save_bytes(user.id, content, filename="image.png", mime=mime, kind="image")
            row = StoredFile(
                user_id=user.id,
                kind="image",
                filename=final_name,
                path=rel,
                mime=mime,
                size=len(content),
                prompt=prompt[:2000],
            )
            session.add(row)
            saved.append(row)
    except StorageFullError:
        await session.rollback()
        for row in saved:
            delete_file(row.path)
        raise
    if saved:
        await session.commit()
    return saved
