"""PicSystem 后端入口。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import EPHEMERAL_SECRET, settings
from .database import init_db
from .routers import (
    admin_invites,
    admin_maintenance,
    admin_overview,
    admin_redeem_codes,
    admin_risk,
    admin_settings,
    admin_upstream,
    admin_users,
    auth,
    chat,
    files,
    images,
    keys,
    misc,
    ppt,
    search,
    v1,
)
from .services.upstream import UpstreamError, close_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("picsystem")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if EPHEMERAL_SECRET:
        logger.warning("未配置 JWT_SECRET，已使用临时密钥（仅适用于本地开发，重启后会话全部失效）")
    await init_db()
    logger.info("PicSystem 后端已启动")
    from .services.cleanup import cleanup_loop

    cleanup_task = asyncio.create_task(cleanup_loop())
    yield
    cleanup_task.cancel()
    await close_client()


app = FastAPI(title="PicSystem", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# /v1 面向 API 调用方，放开 CORS（不带凭据，Cookie 会话不受跨站影响）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """网页端写操作要求自定义头，阻断跨站表单伪造（含登录 CSRF）。"""
    if request.url.path.startswith("/api/") and request.method not in _SAFE_METHODS:
        if "authorization" not in request.headers:
            if request.headers.get("x-requested-with") != "XMLHttpRequest":
                return JSONResponse({"detail": "缺少防伪请求头"}, status_code=403)
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    # /v1 返回 OpenAI 风格错误体
    if request.url.path.startswith("/v1/") and isinstance(detail, dict) and "error" in detail:
        return JSONResponse(detail, status_code=exc.status_code)
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


@app.exception_handler(UpstreamError)
async def upstream_exception_handler(request: Request, exc: UpstreamError):
    status = exc.status_code if 400 <= exc.status_code < 600 else 502
    if request.url.path.startswith("/v1/"):
        return JSONResponse({"error": {"message": exc.message, "type": "upstream_error"}}, status_code=status)
    return JSONResponse({"detail": exc.message}, status_code=status)


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "picsystem", "version": "1.0.0"}


app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(search.router)
app.include_router(images.router)
app.include_router(ppt.router)
app.include_router(files.router)
app.include_router(keys.router)
app.include_router(misc.router)
app.include_router(admin_overview.router)
app.include_router(admin_users.router)
app.include_router(admin_invites.router)
app.include_router(admin_settings.router)
app.include_router(admin_risk.router)
app.include_router(admin_maintenance.router)
app.include_router(admin_upstream.router)
app.include_router(admin_redeem_codes.router)
app.include_router(v1.router)
