"""管理端：站点设置 + 上游服务配置与连通性测试。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as env_settings
from ..database import get_db
from ..deps import require_admin
from ..models import User
from ..schemas import SettingsPatchIn, UpstreamTestIn
from ..services.content_guard import DEFAULT_KEYWORDS
from ..services.settings_store import DEFAULT_AGREEMENT_TEXT, get_all_settings, set_settings
from ..services.upstream import UpstreamError, request_json, test_connection, upstream_config
from ..services.usage import audit

router = APIRouter(prefix="/api/admin", tags=["admin-settings"])


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "*" * (len(secret) - 8) + secret[-4:]


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    all_settings = await get_all_settings(db)
    saved_key = str(all_settings.get("upstream_api_key") or "")
    return {
        "feature_chat_enabled": bool(all_settings.get("feature_chat_enabled", True)),
        "feature_image_enabled": bool(all_settings.get("feature_image_enabled", True)),
        "feature_search_enabled": bool(all_settings.get("feature_search_enabled", True)),
        "feature_ppt_enabled": bool(all_settings.get("feature_ppt_enabled", True)),
        "registration_enabled": bool(all_settings.get("registration_enabled", True)),
        "site_name": str(all_settings.get("site_name") or ""),
        "announcement": str(all_settings.get("announcement") or ""),
        "upstream_base_url": str(all_settings.get("upstream_base_url") or ""),
        "upstream_api_key_masked": _mask(saved_key),
        "env_upstream_base_url": env_settings.upstream_base_url,
        "env_upstream_configured": bool(env_settings.upstream_api_key),
        "user_rate_limit_per_minute": int(all_settings.get("user_rate_limit_per_minute") or 20),
        "login_max_failures": int(all_settings.get("login_max_failures") or 5),
        "login_lock_minutes": int(all_settings.get("login_lock_minutes") or 15),
        "register_max_per_ip_day": int(all_settings.get("register_max_per_ip_day") or 10),
        "storage_quota_mb_default": int(all_settings.get("storage_quota_mb_default") or 0),
        "file_retention_hours": int(all_settings.get("file_retention_hours") or 0),
        "log_retention_hours": int(all_settings.get("log_retention_hours") or 168),
        "audit_retention_days": int(all_settings.get("audit_retention_days") or 90),
        "register_max_per_fp_total": int(all_settings.get("register_max_per_fp_total") or 2),
        "registration_require_approval": bool(all_settings.get("registration_require_approval", False)),
        "agreement_version": int(all_settings.get("agreement_version") or 1),
        "agreement_text": str(all_settings.get("agreement_text") or ""),
        "agreement_text_default": DEFAULT_AGREEMENT_TEXT,
        "notice_version": int(all_settings.get("notice_version") or 1),
        "notice_text": str(all_settings.get("notice_text") or ""),
        "content_filter_enabled": bool(all_settings.get("content_filter_enabled", True)),
        "content_filter_keywords": str(all_settings.get("content_filter_keywords") or ""),
        "content_filter_keywords_default": "\n".join(DEFAULT_KEYWORDS),
        "checkin_enabled": bool(all_settings.get("checkin_enabled", False)),
        "checkin_pool": str(all_settings.get("checkin_pool") or "permanent"),
        "checkin_valid_days": int(all_settings.get("checkin_valid_days") or 1),
        "checkin_valid_hours": int(all_settings.get("checkin_valid_hours") or 0),
        "checkin_timezone": str(all_settings.get("checkin_timezone") or "Asia/Shanghai"),
        "checkin_chat": int(all_settings.get("checkin_chat") or 0),
        "checkin_image": int(all_settings.get("checkin_image") or 0),
        "checkin_search": int(all_settings.get("checkin_search") or 0),
        "checkin_ppt": int(all_settings.get("checkin_ppt") or 0),
        "max_inflight_default": int(all_settings.get("max_inflight_default") or 2),
    }


@router.patch("/settings")
async def patch_settings(
    body: SettingsPatchIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    values = body.model_dump(exclude_none=True)
    changed_keys = []
    payload: dict = {}
    for key, value in values.items():
        if key == "upstream_base_url":
            value = str(value).strip().rstrip("/")
            if value and not value.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="上游地址需以 http:// 或 https:// 开头")
        if key == "upstream_api_key":
            value = str(value).strip()
            # 留空 = 清除覆盖；提交掩码值则保持不变
            if value and set(value) <= {"*"}:
                continue
        payload[key] = value
        changed_keys.append(key)
    if payload:
        # 仅在全局存储默认真正下调时才触发裁剪（比较旧值）
        old_storage_default = None
        if "storage_quota_mb_default" in payload:
            from ..services.settings_store import get_setting
            old_storage_default = int(await get_setting(db, "storage_quota_mb_default") or 0)
        await set_settings(db, payload)
        await db.commit()
        await audit(db, admin_id=admin.id, action="settings.update", detail=",".join(changed_keys))
        if old_storage_default is not None:
            new_default = int(payload["storage_quota_mb_default"])
            if new_default > 0 and (old_storage_default == 0 or new_default < old_storage_default):
                _start_global_storage_trim(new_default, admin.id)
    return await get_settings(db, admin)


_bg_tasks: set[asyncio.Task] = set()
_trim_lock = asyncio.Lock()


def _start_global_storage_trim(limit_mb: int, admin_id: int) -> None:
    from ..database import SessionLocal
    from ..models import User
    from ..services.storage import trim_user_storage
    from sqlalchemy import select

    async def _run() -> None:
        if limit_mb <= 0 or _trim_lock.locked():
            return
        async with _trim_lock:
            total_removed = 0
            async with SessionLocal() as session:
                user_ids = (
                    await session.execute(select(User.id).where(User.storage_limit_mb.is_(None)))
                ).scalars().all()
                for uid in user_ids:
                    try:
                        removed, _freed = await trim_user_storage(session, uid, limit_mb)
                        total_removed += removed
                    except Exception:
                        # 单用户失败不中断整体，回滚该会话后继续
                        import logging
                        logging.getLogger("picsystem.storage").exception("用户 %s 存储裁剪失败", uid)
                        await session.rollback()
                if total_removed:
                    from ..services.usage import audit as write_audit
                    await write_audit(session, admin_id=admin_id, action="storage.global_trim",
                                      detail=f"全局默认下调至 {limit_mb}MB，共裁剪 {total_removed} 个旧文件")

    def _on_done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            import logging
            logging.getLogger("picsystem.storage").error("全局存储裁剪任务异常: %s", t.exception())

    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_on_done)


@router.post("/upstream/test")
async def upstream_test(
    body: UpstreamTestIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """测试上游连通性。未提供参数时使用“保存的覆盖值 → 环境变量”解析结果。"""
    if body.base_url or body.api_key:
        all_settings = await get_all_settings(db)
        base = (body.base_url or str(all_settings.get("upstream_base_url") or "")
                or env_settings.upstream_base_url).rstrip("/")
        key = body.api_key or str(all_settings.get("upstream_api_key") or "") or env_settings.upstream_api_key
    else:
        try:
            base, key = await upstream_config(db)
        except UpstreamError as exc:
            return {"ok": False, "error": exc.message}
    if not base:
        return {"ok": False, "error": "未配置上游服务地址"}
    if not key:
        return {"ok": False, "error": "未配置上游访问密钥"}
    try:
        result = await test_connection(base, key)
        return {**result, "base_url": base}
    except UpstreamError as exc:
        return {"ok": False, "error": exc.message, "base_url": base}


@router.get("/upstream/models")
async def upstream_models(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    try:
        base_url, api_key = await upstream_config(db)
    except UpstreamError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    try:
        return await request_json("GET", base_url, api_key, "/v1/models")
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
