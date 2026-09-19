"""本地文件存储：生成的图片 / PPT / PSD 落盘，经本站鉴权后分发。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

from ..config import settings

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "image/vnd.adobe.photoshop": ".psd",
}

_EXT_WHITELIST = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".zip", ".pptx", ".psd"}


def data_root() -> Path:
    root = Path(settings.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_ext(filename: str, mime: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _EXT_WHITELIST:
        return ext
    return _MIME_EXT.get(mime.split(";")[0].strip().lower(), ".bin")


def save_bytes(user_id: int, content: bytes, *, filename: str, mime: str, kind: str) -> tuple[str, str]:
    """保存文件，返回 (相对路径, 最终文件名)。路径含随机 id，防止猜测与穿越。"""
    ext = _safe_ext(filename, mime)
    today = datetime.now().strftime("%Y%m")
    final_name = f"{uuid.uuid4().hex}{ext}"
    rel = Path("files") / str(user_id) / today / final_name
    abs_path = data_root() / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)
    return str(rel).replace("\\", "/"), final_name


def resolve_path(rel_path: str) -> Path:
    """将存储的相对路径解析为绝对路径，并校验未越出数据目录。"""
    root = data_root()
    candidate = (root / rel_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise PermissionError("非法文件路径")
    return candidate


def dir_usage(root) -> tuple[int, int]:
    """递归统计目录的文件数与总字节（不存在时返回 0,0）。"""
    from pathlib import Path as _Path

    base = _Path(root)
    if not base.exists():
        return 0, 0
    total = 0
    count = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            continue
    return total, count


def delete_file(rel_path: str) -> None:
    """删除产物文件及其缩略图。

    这里是全项目删除产物的唯一收口点（用户删除 / 保留期清理 / 配额回滚 / 删用户），
    缩略图挂在此处可保证不残留；用函数内导入避免与 thumbnails 形成循环依赖。
    """
    from .thumbnails import delete_thumbnail

    delete_thumbnail(rel_path)
    try:
        path = resolve_path(rel_path)
        path.unlink(missing_ok=True)
    except Exception:
        pass


_SAFE_DOWNLOAD_NAME = re.compile(r"[^\w\-.一-鿿]+")


def download_name(name: str) -> str:
    cleaned = _SAFE_DOWNLOAD_NAME.sub("_", name).strip("._")
    return cleaned or "download"


# ---- 用户存储配额 ----

class StorageFullError(Exception):
    """用户存储配额已满。"""


async def user_storage_bytes(session, user_id: int) -> int:
    """用户当前已用存储（字节）。"""
    from sqlalchemy import func, select

    from ..models import StoredFile

    return await session.scalar(
        select(func.coalesce(func.sum(StoredFile.size), 0)).where(StoredFile.user_id == user_id)
    ) or 0


async def effective_storage_limit_mb(session, user) -> int:
    """用户生效的存储上限（MB）：个人设置优先，否则全局默认；0 = 不限。"""
    from .settings_store import get_setting

    if getattr(user, "storage_limit_mb", None) is not None:
        return int(user.storage_limit_mb)
    try:
        return int(await get_setting(session, "storage_quota_mb_default") or 0)
    except (TypeError, ValueError):
        return 0


async def ensure_storage_capacity(session, user, incoming_bytes: int) -> None:
    """保存产物前检查配额，超限抛 StorageFullError（由路由转为 403）。"""
    limit_mb = await effective_storage_limit_mb(session, user)
    if limit_mb <= 0:
        return
    used = await user_storage_bytes(session, user.id)
    if used + incoming_bytes > limit_mb * 1024 * 1024:
        raise StorageFullError(
            f"存储空间不足（上限 {limit_mb}MB，已用 {used // 1024 // 1024}MB），请清理文件或联系管理员"
        )


async def trim_user_storage(session, user_id: int, limit_mb: int, *, commit: bool = True) -> tuple[int, int]:
    """把用户存储裁剪到 limit_mb 以内：按创建时间从新到旧保留，
    删除最旧的超出部分。返回 (删除文件数, 释放字节数)。limit<=0 时不处理。

    用 core 批量删除，容忍与清理任务并发时行已消失的情况。
    """
    from sqlalchemy import delete as sql_delete
    from sqlalchemy import select

    from ..models import StoredFile

    if limit_mb <= 0:
        return 0, 0
    budget = limit_mb * 1024 * 1024
    rows = (
        await session.execute(
            select(StoredFile)
            .where(StoredFile.user_id == user_id)
            .order_by(StoredFile.created_at.desc(), StoredFile.id.desc())
        )
    ).scalars().all()
    kept = 0
    drop_ids: list[int] = []
    drop_paths: list[str] = []
    removed_bytes = 0
    for row in rows:
        if kept + row.size <= budget:
            kept += row.size
            continue
        drop_ids.append(row.id)
        drop_paths.append(row.path)
        removed_bytes += row.size
    if drop_ids:
        for path in drop_paths:
            delete_file(path)
        await session.execute(sql_delete(StoredFile).where(StoredFile.id.in_(drop_ids)))
        if commit:
            await session.commit()
    return len(drop_ids), removed_bytes
