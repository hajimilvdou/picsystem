"""图片缩略图：按需生成、落盘缓存、随原文件一起删除。

设计要点：
- 缩略图放在 `DATA_DIR/thumbs/<原相对路径，后缀换 .jpg>`，与 `DATA_DIR/files/` **分开存放**，
  否则 `cleanup_orphan_files` 会把数据里没登记的缩略图当成孤儿文件删掉；
- 惰性生成：只有被请求时才做，已存在直接复用（历史文件无需批量回填）；
- 一律输出 JPEG（透明度铺白底），避免依赖 Pillow 的 WebP/AVIF 编译选项；
- 任何解码失败都返回 None（前端回退到原图），绝不让绘图链路 500。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from ..config import MAX_DECODE_PIXELS, settings

log = logging.getLogger("picsystem.thumbnails")

THUMB_EDGE = 400          # 长边像素
THUMB_QUALITY = 82
MAX_SOURCE_BYTES = 64 * 1024 * 1024   # 超过则不生成，防止解码超大图打爆内存

# 可生成缩略图的 MIME（PPT/PSD/PDF/ZIP 等交给前端显示图标）
THUMBABLE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def thumbs_root() -> Path:
    root = Path(settings.data_dir).resolve() / "thumbs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def thumb_abs_path(rel_path: str) -> Path:
    """由原文件相对路径推导缩略图绝对路径（后缀统一 .jpg）。"""
    rel = Path(str(rel_path))
    return thumbs_root() / rel.with_suffix(".jpg")


def delete_thumbnail(rel_path: str) -> None:
    try:
        thumb_abs_path(rel_path).unlink(missing_ok=True)
    except Exception:
        pass


def ensure_thumbnail(source: Path, rel_path: str) -> Path | None:
    """确保缩略图存在并返回其路径；不需要或无法生成时返回 None。"""
    dest = thumb_abs_path(rel_path)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp: Path | None = None
    try:
        if not source.exists():
            return None
        size_bytes = source.stat().st_size
        if size_bytes > MAX_SOURCE_BYTES:
            log.warning("跳过缩略图（源文件 %.1fMB 超过上限）：%s", size_bytes / 1048576, rel_path)
            return None
        with Image.open(source) as image:
            width, height = image.size
            if width * height > MAX_DECODE_PIXELS:
                log.warning(
                    "跳过缩略图（%dx%d 超过解码像素上限）：%s", width, height, rel_path,
                )
                return None
            # 即使是小图也统一重编码：400px 的 JPEG 通常比原 PNG 小得多，
            # 这样 /thumb 对任何合法图片都不会 404，前端无需退化逻辑
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                canvas = Image.new("RGB", image.size, (255, 255, 255))
                canvas.paste(image, mask=image.split()[-1])
                image = canvas
            else:
                image = image.convert("RGB")
            image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再原子替换：并发请求同一张图时不会读到写了一半的文件。
            # 临时名带随机后缀——两个请求同时生成同一张缩略图时，用固定名会互相覆盖/改名失败，
            # 导致其中一个拿到 404（前端只能回退原图，白白多发一次请求）。
            tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
            image.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True)
            tmp.replace(dest)
            tmp = None
        return dest
    except Exception as exc:  # noqa: BLE001 - 缩略图失败不能影响原图访问
        log.warning("生成缩略图失败（%s）：%s", rel_path, exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return None
