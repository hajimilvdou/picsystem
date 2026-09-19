"""管理端：用户管理（列表 / 新建 / 编辑额度与状态 / 重置密码 / 删除 / 审核 / 批量额度 / 统计）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import (
    FEATURE_LABELS,
    InviteCode,
    StoredFile,
    UsageLog,
    User,
    UserQuota,
    UserRole,
    UserSession,
    UserStatus,
)
from ..schemas import AdminBulkQuotaIn, AdminResetPasswordIn, AdminUserCreateIn, AdminUserPatchIn
from ..security import generate_password, hash_password
from ..services.quota import compute_temp_expiry, get_quotas
from ..services.sessions import revoke_all_sessions
from ..services.settings_store import get_setting
from ..services.storage import delete_file, trim_user_storage, user_storage_bytes
from ..services.usage import audit

router = APIRouter(prefix="/api/admin/users", tags=["admin-users"])


def _user_out(user: User, quotas: dict, invite_code: str | None = None, storage_bytes: int = 0) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "status": user.status,
        "invite_code": invite_code,
        "quotas": quotas,
        "storage_limit_mb": user.storage_limit_mb,
        "storage_bytes": storage_bytes,
        "max_inflight": user.max_inflight,
        "reg_note": user.reg_note,
        "note": user.note,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


@router.get("")
async def list_users(
    q: str = Query(default=""),
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    cond = or_(User.username.contains(q), User.id == int(q)) if q and q.isdigit() else (
        User.username.contains(q) if q else None
    )
    query = select(User)
    count_q = select(func.count(User.id))
    if cond is not None:
        query = query.where(cond)
        count_q = count_q.where(cond)
    if status:
        query = query.where(User.status == status)
        count_q = count_q.where(User.status == status)
    total = await db.scalar(count_q) or 0
    pending_count = await db.scalar(
        select(func.count(User.id)).where(User.status == UserStatus.PENDING.value)
    ) or 0
    result = await db.execute(query.order_by(User.id.desc()).offset((page - 1) * size).limit(size))
    items = []
    for user in result.scalars():
        quotas = await get_quotas(db, user.id)
        code = None
        if user.invite_code_id:
            invite = await db.get(InviteCode, user.invite_code_id)
            code = invite.code if invite else None
        storage_bytes = await user_storage_bytes(db, user.id)
        items.append(_user_out(user, quotas, code, storage_bytes))
    return {"total": total, "pending_count": pending_count, "items": items, "feature_labels": FEATURE_LABELS}


@router.post("")
async def create_user(
    body: AdminUserCreateIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from None
    for feature in ("chat", "image", "search", "ppt"):
        db.add(UserQuota(user_id=user.id, feature=feature, quota_total=body.quotas.get(feature, 0)))
    await db.commit()
    await audit(db, admin_id=admin.id, action="user.create", target=user.username,
                detail=f"role={user.role}")
    return _user_out(user, await get_quotas(db, user.id))


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    body: AdminUserPatchIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await _get_user(db, user_id)
    changes = []
    if body.role is not None and body.role != user.role:
        if user.id == admin.id and body.role != UserRole.ADMIN.value:
            raise HTTPException(status_code=400, detail="不能取消自己的管理员权限")
        user.role = body.role
        changes.append(f"role→{body.role}")
    if body.status is not None and body.status != user.status:
        if user.id == admin.id:
            raise HTTPException(status_code=400, detail="不能禁用自己的账号")
        user.status = body.status
        user.token_version += 1  # 禁用后立即失效其会话
        if body.status == "disabled":
            await revoke_all_sessions(db, user.id)
        changes.append(f"status→{body.status}")
    if body.quotas:
        for feature, patch in body.quotas.items():
            if feature not in FEATURE_LABELS:
                continue
            row = await db.get(UserQuota, {"user_id": user.id, "feature": feature})
            if row is None:
                row = UserQuota(user_id=user.id, feature=feature)
                db.add(row)
            if patch.total is not None and patch.total != row.quota_total:
                row.quota_total = patch.total
                changes.append(f"{feature}.total→{patch.total}")
            if patch.reset_used:
                row.quota_used = 0
                changes.append(f"{feature}.used→0")
    # 限额确实调低到当前用量以下时：保留最新文件，自动删除最旧超出部分（随请求统一提交）
    storage_changed = (body.storage_limit_clear and user.storage_limit_mb is not None) or (
        body.storage_limit_mb is not None and body.storage_limit_mb != user.storage_limit_mb
    )
    if body.storage_limit_clear:
        user.storage_limit_mb = None
        changes.append("storage→跟随全局")
    elif body.storage_limit_mb is not None and body.storage_limit_mb != user.storage_limit_mb:
        user.storage_limit_mb = body.storage_limit_mb
        changes.append(f"storage→{body.storage_limit_mb}MB")
    if storage_changed:
        effective_limit = user.storage_limit_mb
        if effective_limit is None:
            effective_limit = int(await get_setting(db, "storage_quota_mb_default") or 0)
        if effective_limit and effective_limit > 0:
            removed, freed = await trim_user_storage(db, user.id, effective_limit, commit=False)
            if removed:
                changes.append(f"裁剪旧文件 {removed} 个，释放 {freed // 1024 // 1024}MB")
    if body.max_inflight_clear:
        user.max_inflight = None
        changes.append("inflight→跟随全局")
    elif body.max_inflight is not None and body.max_inflight != user.max_inflight:
        user.max_inflight = body.max_inflight
        changes.append(f"inflight→{body.max_inflight}")
    if body.note is not None and body.note.strip() != user.note:
        user.note = body.note.strip()[:500]
        changes.append("备注已更新")
    await db.commit()
    if changes:
        await audit(db, admin_id=admin.id, action="user.update", target=user.username,
                    detail="; ".join(changes)[:900])
    return _user_out(user, await get_quotas(db, user.id), storage_bytes=await user_storage_bytes(db, user.id))


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    body: AdminResetPasswordIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await _get_user(db, user_id)
    new_password = body.password or generate_password()
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="密码长度至少 8 位")
    user.password_hash = hash_password(new_password)
    user.token_version += 1
    await revoke_all_sessions(db, user.id)
    await db.commit()
    await audit(db, admin_id=admin.id, action="user.reset_password", target=user.username)
    return {"password": new_password}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await _get_user(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    # 清理磁盘文件；数据库行由外键级联删除
    files = (await db.execute(select(StoredFile).where(StoredFile.user_id == user.id))).scalars().all()
    for row in files:
        delete_file(row.path)
    await db.delete(user)
    await db.commit()
    await audit(db, admin_id=admin.id, action="user.delete", target=user.username)
    return {"ok": True}


# ---- 审批制注册 ----

@router.post("/{user_id}/approve")
async def approve_user(user_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    user = await _get_user(db, user_id)
    if user.status != UserStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="该用户不在待审核状态")
    user.status = UserStatus.ACTIVE.value
    await db.commit()
    await audit(db, admin_id=admin.id, action="user.approve", target=user.username,
                detail=f"申请备注：{user.reg_note[:100]}")
    return {"ok": True}


@router.post("/{user_id}/reject")
async def reject_user(user_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    user = await _get_user(db, user_id)
    # 条件更新占位，防并发双拒绝导致邀请码次数重复回退
    claimed = await db.execute(
        update(User)
        .where(User.id == user.id, User.status == UserStatus.PENDING.value)
        .values(status="rejected")
    )
    if claimed.rowcount != 1:
        raise HTTPException(status_code=400, detail="该用户不在待审核状态")
    if user.invite_code_id:
        await db.execute(
            update(InviteCode)
            .where(InviteCode.id == user.invite_code_id)
            .where(InviteCode.used_count > 0)
            .values(used_count=InviteCode.used_count - 1)
        )
    await db.delete(user)
    await db.commit()
    await audit(db, admin_id=admin.id, action="user.reject", target=user.username)
    return {"ok": True}


# ---- 批量额度调整 ----

@router.post("/bulk-quota")
async def bulk_quota(body: AdminBulkQuotaIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    if isinstance(body.user_ids, str):
        if body.user_ids != "all":
            raise HTTPException(status_code=400, detail="user_ids 需为数组或 all")
        ids = (await db.execute(select(User.id))).scalars().all()
    else:
        ids = body.user_ids[:1000]
    if not ids:
        raise HTTPException(status_code=400, detail="没有目标用户")

    tz_name = str(await get_setting(db, "checkin_timezone") or "Asia/Shanghai")
    if body.pool == "temporary":
        if body.fixed_expires_at is not None:
            expires_at = body.fixed_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = compute_temp_expiry(tz_name, body.valid_days, body.valid_hours)
    else:
        expires_at = None
    affected = 0
    for uid in ids:
        row = await db.get(UserQuota, {"user_id": uid, "feature": body.feature})
        if row is None:
            row = UserQuota(user_id=uid, feature=body.feature)
            db.add(row)
            await db.flush()
        if body.mode == "set":
            if body.pool == "permanent":
                if row.quota_total >= 0:  # 不覆盖“不限”账号
                    row.quota_total = body.amount
                    affected += 1
            else:
                row.temp_amount = body.amount
                row.temp_expires_at = expires_at
                affected += 1
        elif body.mode == "add":
            if body.pool == "permanent":
                if row.quota_total >= 0:
                    row.quota_total += body.amount
                    affected += 1
            else:
                row.temp_amount += body.amount
                current = row.temp_expires_at
                if current is not None and current.tzinfo is None:
                    current = current.replace(tzinfo=timezone.utc)
                row.temp_expires_at = max(current, expires_at) if current else expires_at
                affected += 1
        else:  # subtract
            if body.pool == "permanent":
                if row.quota_total >= 0:
                    row.quota_total = max(row.quota_total - body.amount, 0)
                    row.quota_used = min(row.quota_used, row.quota_total)
                    affected += 1
            else:
                row.temp_amount = max(row.temp_amount - body.amount, 0)
                affected += 1
    await db.commit()
    await audit(
        db, admin_id=admin.id, action="user.bulk_quota",
        detail=f"{body.feature}/{body.pool}/{body.mode} {body.amount} × {affected} 用户",
    )
    return {"ok": True, "affected": affected}


# ---- 用户操作统计（不含具体内容）----

@router.get("/{user_id}/stats")
async def user_stats(user_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    user = await _get_user(db, user_id)
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    totals = await db.execute(
        select(UsageLog.feature, func.count(UsageLog.id),
               func.coalesce(func.sum(case((UsageLog.status == "success", 1), else_=0)), 0))
        .where(UsageLog.user_id == user_id)
        .group_by(UsageLog.feature)
    )
    week = await db.execute(
        select(func.count(UsageLog.id)).where(UsageLog.user_id == user_id, UsageLog.created_at >= week_ago)
    )
    last_log = await db.scalar(
        select(func.max(UsageLog.created_at)).where(UsageLog.user_id == user_id)
    )
    daily = await db.execute(
        select(func.date(UsageLog.created_at).label("day"), func.count(UsageLog.id))
        .where(UsageLog.user_id == user_id, UsageLog.created_at >= week_ago)
        .group_by("day").order_by("day")
    )
    sessions_count = await db.scalar(
        select(func.count(UserSession.id)).where(
            UserSession.user_id == user_id, UserSession.revoked.is_(False)
        )
    )
    return {
        "username": user.username,
        "totals": {f: {"count": c, "success": s} for f, c, s in totals.all()},
        "week_count": week.scalar() or 0,
        "daily": [{"day": str(d), "count": c} for d, c in daily.all()],
        "last_active": last_log,
        "active_sessions": sessions_count or 0,
        "storage_bytes": await user_storage_bytes(db, user_id),
        "quotas": await get_quotas(db, user_id),
    }
