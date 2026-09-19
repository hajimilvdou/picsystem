"""注册 / 登录 / 会话 / 设备管理 / 免责协议。"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..deps import client_ip, get_current_user, rate_limit, user_rate_limit
from ..models import InviteCode, Redemption, RedemptionCode, User, UserRole, UserSession, UserStatus
from ..schemas import ChangePasswordIn, LoginIn, MeOut, QuotaInfo, RedeemIn, RegisterIn, StorageInfo
from ..security import (
    create_session_token,
    decode_session_token,
    generate_password,
    hash_password,
    verify_password,
)
from ..services.checkin import do_checkin, has_checked_in_today
from ..services.quota import get_quotas
from ..services.redeem import apply_invite_grants
from ..services.risk import login_protector, record_event, registrations_today
from ..services.sessions import (
    create_session,
    fingerprint_hash,
    list_sessions,
    revoke_all_sessions,
    revoke_session,
)
from ..services.settings_store import DEFAULT_AGREEMENT_TEXT, feature_flags, get_setting
from ..services.storage import effective_storage_limit_mb, user_storage_bytes

logger = logging.getLogger("picsystem.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _random_credentials() -> tuple[str, str]:
    username = "u_" + secrets.token_hex(4)
    return username, generate_password(12)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        path="/",
    )


@router.post("/register", dependencies=[Depends(rate_limit("register", 10, 60))])
async def register(body: RegisterIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    if not await get_setting(db, "registration_enabled"):
        raise HTTPException(status_code=403, detail="注册已关闭")

    ip = client_ip(request)
    daily_limit = int(await get_setting(db, "register_max_per_ip_day") or 10)
    if await registrations_today(db, ip) >= daily_limit:
        await record_event(db, kind="register_blocked", username=body.username or "(随机)", ip=ip,
                           detail=f"超过单 IP 每日注册上限 {daily_limit}")
        raise HTTPException(status_code=429, detail="该 IP 今日注册次数已达上限，请明天再试")

    # 设备指纹风控：防单人批量注册抢占名额（指纹仅存加盐哈希）
    if not body.fp:
        await record_event(db, kind="register_blocked", username=body.username or "(随机)", ip=ip,
                           detail="缺少设备指纹")
        raise HTTPException(status_code=400, detail="注册环境异常，请通过网页端注册")
    fp = fingerprint_hash(body.fp)
    fp_count = await db.scalar(select(func.count(User.id)).where(User.reg_fp == fp)) or 0
    fp_limit = int(await get_setting(db, "register_max_per_fp_total") or 2)
    if fp_count >= fp_limit:
        await record_event(db, kind="register_blocked", username=body.username or "(随机)", ip=ip,
                           detail=f"设备指纹已达注册上限 {fp_limit}")
        raise HTTPException(status_code=429, detail="当前设备注册账号数量已达上限")
    fp_used_invite = await db.scalar(
        select(func.count(User.id)).where(User.reg_fp == fp).where(
            User.invite_code_id == select(InviteCode.id).where(InviteCode.code == body.invite_code).scalar_subquery()
        )
    ) or 0
    if fp_used_invite >= 1:
        await record_event(db, kind="register_blocked", username=body.username or "(随机)", ip=ip,
                           detail="同一设备重复使用同一邀请码")
        raise HTTPException(status_code=429, detail="当前设备已使用过该邀请码")

    # 自定义或随机生成凭证（随机凭证仅在响应中返回一次）。
    # 用户名先查重（随机模式撞名重试 3 次），避免邀请码次数被无效扣减。
    generated = False
    username = body.username
    password = body.password
    if not username and not password:
        generated = True
        for _ in range(3):
            username, password = _random_credentials()
            if not await db.scalar(select(func.count(User.id)).where(User.username == username)):
                break
        else:
            raise HTTPException(status_code=409, detail="系统繁忙，请重试")
    else:
        if await db.scalar(select(func.count(User.id)).where(User.username == username)):
            raise HTTPException(status_code=409, detail="用户名已被注册")

    now = datetime.now(timezone.utc)
    stmt = (
        update(InviteCode)
        .where(InviteCode.code == body.invite_code)
        .where(InviteCode.enabled.is_(True))
        .where(InviteCode.used_count < InviteCode.max_uses)
        .where((InviteCode.expires_at.is_(None)) | (InviteCode.expires_at > now))
        .values(used_count=InviteCode.used_count + 1)
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    if result.rowcount != 1:
        raise HTTPException(status_code=400, detail="邀请码无效、已用完、已过期或已被禁用")
    invite = (await db.execute(select(InviteCode).where(InviteCode.code == body.invite_code))).scalar_one()

    user = User(
        username=username,
        password_hash=hash_password(password),
        role=UserRole.USER.value,
        status=UserStatus.PENDING.value
        if await get_setting(db, "registration_require_approval")
        else UserStatus.ACTIVE.value,
        invite_code_id=invite.id,
        reg_fp=fp,
        reg_ip=ip,
        reg_note=body.note[:300],
        note=body.note[:500],  # 管理员备注自动带入申请备注
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="用户名已被注册") from None

    # 按邀请码的池类型发放注册初始额度（限时组按 天数+小时 计算到期，时区取签到设置）
    await apply_invite_grants(db, user, invite)
    await db.commit()
    logger.info("新用户注册：%s（邀请码 #%s，IP %s）", user.username, invite.id, ip)
    await record_event(db, kind="register", username=user.username, user_id=user.id, ip=ip,
                       detail=f"邀请码 {invite.code}" + ("，待审核" if user.status == "pending" else ""))

    ua = request.headers.get("user-agent", "")
    session = await create_session(db, user_id=user.id, ip=ip, user_agent=ua)
    await db.commit()
    token = create_session_token(user.id, user.role, user.token_version, session.id)
    _set_session_cookie(response, token)
    return {"ok": True, "pending": user.status == "pending", "generated": generated,
            "username": username if generated else None,
            "password": password if generated else None}


@router.post("/login", dependencies=[Depends(rate_limit("login", 10, 60))])
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    locked = await login_protector.locked_remaining(body.username, ip)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"失败次数过多，账号已临时锁定，请 {max(locked // 60 + 1, 1)} 分钟后再试",
        )

    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        max_failures = int(await get_setting(db, "login_max_failures") or 5)
        lock_minutes = int(await get_setting(db, "login_lock_minutes") or 15)
        triggered = await login_protector.register_failure(
            body.username, ip, max_failures=max_failures, lock_seconds=lock_minutes * 60
        )
        await record_event(
            db,
            kind="login_locked" if triggered else "login_fail",
            username=body.username,
            ip=ip,
            detail=f"连续失败触发锁定 {lock_minutes} 分钟" if triggered else "用户名或密码错误",
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status == UserStatus.DISABLED.value:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    await login_protector.clear(body.username, ip)
    user.last_login_at = datetime.now(timezone.utc)
    ua = request.headers.get("user-agent", "")
    session = await create_session(db, user_id=user.id, ip=ip, user_agent=ua)
    await db.commit()

    token = create_session_token(user.id, user.role, user.token_version, session.id)
    _set_session_cookie(response, token)
    return {"ok": True, "role": user.role, "pending": user.status == "pending"}


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # 手动解析会话（容忍过期 token），吊销对应设备会话
    token = request.cookies.get(settings.cookie_name)
    if token:
        payload = decode_session_token(token)
        sid = str(payload.get("sid") or "") if payload else ""
        if sid:
            session = await db.get(UserSession, sid)
            if session is not None and not session.revoked:
                session.revoked = True
                await db.commit()
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/public-config", dependencies=[Depends(rate_limit("public_config", 30, 60))])
async def public_config(db: AsyncSession = Depends(get_db)):
    """匿名可读的站点公开配置：登录/注册页据此展示站点名与注册开关。"""
    return {
        "site_name": str(await get_setting(db, "site_name") or settings.site_name),
        "registration_enabled": bool(await get_setting(db, "registration_enabled")),
    }


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    quotas = await get_quotas(db, user.id)
    flags = await feature_flags(db)
    checkin_enabled = bool(await get_setting(db, "checkin_enabled"))
    return MeOut(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        quotas={
            k: QuotaInfo(total=v["total"], used=v["used"], temp=v["temp"], temp_expires_at=v["temp_expires_at"])
            for k, v in quotas.items()
        },
        storage=StorageInfo(
            used_bytes=await user_storage_bytes(db, user.id),
            limit_mb=await effective_storage_limit_mb(db, user),
        ),
        features=flags,
        registration_enabled=bool(await get_setting(db, "registration_enabled")),
        site_name=str(await get_setting(db, "site_name") or settings.site_name),
        announcement=str(await get_setting(db, "announcement") or ""),
        agreement_version=user.agreement_version,
        agreement_required=int(await get_setting(db, "agreement_version") or 1),
        checkin_enabled=checkin_enabled,
        checked_in_today=checkin_enabled and await has_checked_in_today(db, user),
    )


@router.post("/change-password")
async def change_password(
    body: ChangePasswordIn,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 防持会话者在线爆破旧密码：每用户 5 次/分钟
    from ..deps import rate_limiter
    if not await rate_limiter.hit(f"chpwd:{user.id}", 5, 60):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # 使其他会话失效
    current = getattr(request.state, "user_session", None)
    assert current is not None  # get_current_user 必然已设置
    await revoke_all_sessions(db, user.id, except_id=current.id)
    await db.commit()
    token = create_session_token(user.id, user.role, user.token_version, current.id)
    _set_session_cookie(response, token)
    return {"ok": True}


# ---- 登录设备管理 ----

@router.get("/sessions")
async def my_sessions(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    current = getattr(request.state, "user_session", None)
    rows = await list_sessions(db, user.id, active_within=timedelta(minutes=settings.jwt_expire_minutes))
    return {
        "items": [
            {
                "id": s.id,
                "ip": s.ip,
                "device_label": s.device_label,
                "current": current is not None and s.id == current.id,
                "created_at": s.created_at,
                "last_seen_at": s.last_seen_at,
            }
            for s in rows
        ]
    }


@router.delete("/sessions/{session_id}")
async def revoke_my_session(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    current = getattr(request.state, "user_session", None)
    if current is not None and session_id == current.id:
        raise HTTPException(status_code=400, detail="不能注销当前设备，请直接退出登录")
    ok = await revoke_session(db, user.id, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="设备会话不存在")
    return {"ok": True}


# ---- 兑换码（纯额度，老用户可用）----

@router.post("/redeem", dependencies=[Depends(user_rate_limit("redeem"))])
async def redeem(body: RedeemIn, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(RedemptionCode)
        .where(RedemptionCode.code == body.code)
        .where(RedemptionCode.enabled.is_(True))
        .where(RedemptionCode.used_count < RedemptionCode.max_uses)
        .where((RedemptionCode.expires_at.is_(None)) | (RedemptionCode.expires_at > now))
        .values(used_count=RedemptionCode.used_count + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=400, detail="兑换码无效、已用完、已过期或已被禁用")
    code_row = (await db.execute(select(RedemptionCode).where(RedemptionCode.code == body.code))).scalar_one()

    # 每用户对同一码只能兑换一次（唯一约束兜底防并发）
    db.add(Redemption(user_id=user.id, redemption_code_id=code_row.id))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="你已兑换过该兑换码") from None

    granted = await apply_invite_grants(db, user, code_row)
    await db.commit()
    logger.info("用户兑换：%s（兑换码 #%s，IP %s）", user.username, code_row.id, ip)
    try:
        await record_event(db, kind="redeem", username=user.username, user_id=user.id, ip=ip,
                           detail=f"兑换码 {code_row.code}")
    except Exception:
        logger.exception("兑换风控事件记录失败（兑换已成功）")
    return {"ok": True, "granted": granted}


# ---- 每日签到 ----

@router.post("/checkin")
async def checkin(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not await get_setting(db, "checkin_enabled"):
        raise HTTPException(status_code=403, detail="签到功能未开启")
    try:
        granted = await do_checkin(db, user)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if not granted:
        return {"ok": True, "granted": {}, "message": "签到成功（管理员未配置奖励额度）"}
    return {"ok": True, "granted": granted}


# ---- 公告 ----

@router.get("/notice")
async def get_notice(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    text = str(await get_setting(db, "notice_text") or "").strip()
    version = int(await get_setting(db, "notice_version") or 1)
    return {
        "version": version,
        "text": text,
        "show": bool(text) and user.notice_version < version,
    }


@router.post("/notice/ack")
async def ack_notice(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user.notice_version = int(await get_setting(db, "notice_version") or 1)
    await db.commit()
    return {"ok": True}

@router.get("/agreement")
async def get_agreement(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    text = str(await get_setting(db, "agreement_text") or "").strip() or DEFAULT_AGREEMENT_TEXT
    version = int(await get_setting(db, "agreement_version") or 1)
    return {
        "version": version,
        "text": text,
        "accepted": user.agreement_version >= version,
        "accepted_version": user.agreement_version,
    }


@router.post("/agreement/accept")
async def accept_agreement(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    version = int(await get_setting(db, "agreement_version") or 1)
    user.agreement_version = version
    await db.commit()
    # 留痕：同意时间、账号、IP（法律举证）
    await record_event(db, kind="agreement_accept", username=user.username, user_id=user.id,
                       ip=client_ip(request), detail=f"同意协议版本 v{version}")
    return {"ok": True, "version": version}
