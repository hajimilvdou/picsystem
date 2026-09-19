"""图片压缩：把产物重新编码为更小的 WebP（可选限制长边）。

⚠️ **破坏性操作**：压缩会替换原文件，原图不可恢复。因此接口默认 `dry_run=True`，
调用方必须先拿到预估结果、二次确认后才会真正落盘。

约定：
- 只处理 PNG / JPEG / WebP；**GIF 不处理**（重编码会丢动画），PPT/PSD/PDF 等更不碰；
- 统一输出 WebP：保留透明通道，体积通常比原 PNG 小很多，浏览器全面支持；
- 压缩后若反而更大，调用方应跳过写入（见 routers/files.py 的判断）。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, features

log = logging.getLogger("picsystem.image_compress")

COMPRESSIBLE_MIME = {"image/png", "image/jpeg", "image/webp"}
OUTPUT_MIME = "image/webp"
OUTPUT_SUFFIX = ".webp"

DEFAULT_QUALITY = 82
MIN_QUALITY = 40
MAX_QUALITY = 95
MIN_MAX_EDGE = 128
MAX_MAX_EDGE = 8192
MAX_SOURCE_BYTES = 32 * 1024 * 1024

WEBP_AVAILABLE = bool(features.check("webp"))


class CompressError(RuntimeError):
    """压缩失败（解码异常、编码器缺失等）。"""


@dataclass(frozen=True)
class Compressed:
    content: bytes
    width: int
    height: int

    @property
    def size(self) -> int:
        return len(self.content)


def compress(source: Path, *, quality: int = DEFAULT_QUALITY, max_edge: int | None = None) -> Compressed:
    """读原图并编码为 WebP；失败抛 CompressError。"""
    if not WEBP_AVAILABLE:
        raise CompressError("当前 Pillow 构建缺少 WebP 支持，无法压缩")
    if not source.exists():
        raise CompressError("原文件不存在")
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")   # 保留透明通道
            else:
                image = image.convert("RGB")
            if max_edge:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            # method=4 在压缩率与耗时之间比较平衡
            image.save(buffer, "WEBP", quality=quality, method=4)
            return Compressed(content=buffer.getvalue(), width=image.size[0], height=image.size[1])
    except CompressError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise CompressError(f"压缩失败（{exc.__class__.__name__}）") from exc


def output_rel_path(rel_path: str) -> str:
    """压缩后新文件的相对路径（后缀换成 .webp）。"""
    return str(Path(str(rel_path)).with_suffix(OUTPUT_SUFFIX)).replace("\\", "/")


def output_filename(filename: str) -> str:
    """压缩后新文件名（后缀换成 .webp）。"""
    return Path(str(filename or "image")).with_suffix(OUTPUT_SUFFIX).name
