"""上游 chatgpt2api 客户端：统一鉴权、超时、错误映射。

上游地址与密钥：管理后台 settings 表可覆盖环境变量默认值，
便于在内置 / 外部上游之间在线切换。
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .settings_store import get_setting


class UpstreamError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


_TIMEOUT = httpx.Timeout(
    connect=settings.upstream_connect_timeout,
    read=settings.upstream_read_timeout,
    write=60.0,
    pool=30.0,
)
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def upstream_config(session: AsyncSession) -> tuple[str, str]:
    """返回 (base_url, api_key)，settings 覆盖优先于环境变量。"""
    base = (await get_setting(session, "upstream_base_url")) or settings.upstream_base_url
    key = (await get_setting(session, "upstream_api_key")) or settings.upstream_api_key
    base = str(base).rstrip("/")
    if not base:
        raise UpstreamError("未配置上游服务地址", 500)
    if not key:
        raise UpstreamError("未配置上游访问密钥", 500)
    return base, str(key)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _extract_error(body: bytes, status: int) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        detail = data.get("detail") or data.get("error") or data
        if isinstance(detail, dict):
            return str(detail.get("error") or detail.get("message") or detail)[:400]
        return str(detail)[:400]
    except Exception:
        return f"上游返回 HTTP {status}"


async def request_json(
    method: str,
    base_url: str,
    api_key: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    data: dict | None = None,
    files: list | None = None,
) -> dict:
    client = get_client()
    try:
        resp = await client.request(
            method,
            f"{base_url}{path}",
            headers=_auth_headers(api_key),
            json=json_body,
            params=params,
            data=data,
            files=files,
        )
    except httpx.TimeoutException as exc:
        raise UpstreamError("上游服务响应超时") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"无法连接上游服务：{exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        raise UpstreamError(_extract_error(resp.content, resp.status_code), resp.status_code)
    try:
        return resp.json()
    except Exception as exc:
        raise UpstreamError("上游响应不是有效的 JSON") from exc


async def test_connection(base_url: str, api_key: str) -> dict:
    """连通性测试：请求 /v1/models，返回延迟与模型数。"""
    base_url = base_url.rstrip("/")
    started = time.perf_counter()
    data = await request_json("GET", base_url, api_key, "/v1/models")
    latency = int((time.perf_counter() - started) * 1000)
    models = data.get("data") if isinstance(data, dict) else None
    return {
        "ok": True,
        "latency_ms": latency,
        "models_count": len(models) if isinstance(models, list) else 0,
    }


async def stream_chat(
    base_url: str, api_key: str, payload: dict
) -> AsyncIterator[tuple[str, dict | str]]:
    """流式对话。yield ("delta", text) / ("usage", dict) / ("done", "")；
    出错抛 UpstreamError。"""
    client = get_client()
    payload = {**payload, "stream": True}
    try:
        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            headers={**_auth_headers(api_key), "Accept": "text/event-stream"},
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise UpstreamError(_extract_error(body, resp.status_code), resp.status_code)
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    yield ("done", "")
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    yield ("usage", usage)
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield ("delta", str(delta))
    except httpx.TimeoutException as exc:
        raise UpstreamError("上游服务响应超时") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"上游连接中断：{exc.__class__.__name__}") from exc
    yield ("done", "")


_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200MB 上限，防恶意上游撑爆内存/磁盘


async def download_upstream_file(base_url: str, api_key: str, url_or_path: str) -> tuple[bytes, str]:
    """下载上游 /files/... 资源，返回 (内容, Content-Type)。

    安全：仅对与 base_url 同源的地址附带 Authorization；跨源地址不携带任何
    凭证（防上游密钥外泄），且仅允许 http/https，响应体有大小上限。
    """
    base_url = base_url.rstrip("/")
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        url = url_or_path
    elif url_or_path.startswith("/"):
        url = f"{base_url}{url_or_path}"
    else:
        url = f"{base_url}/{url_or_path}"

    same_origin = url.startswith(base_url + "/")
    headers = _auth_headers(api_key) if same_origin else {}

    client = get_client()
    try:
        async with client.stream("GET", url, headers=headers, timeout=180.0) as resp:
            if resp.status_code >= 400:
                raise UpstreamError(f"下载上游文件失败（HTTP {resp.status_code}）", resp.status_code)
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes(1024 * 1024):
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    raise UpstreamError("上游文件超过 200MB 上限")
                chunks.append(chunk)
            content_type = resp.headers.get("content-type", "application/octet-stream")
    except httpx.HTTPError as exc:
        raise UpstreamError(f"下载上游文件失败：{exc.__class__.__name__}") from exc
    return b"".join(chunks), content_type
