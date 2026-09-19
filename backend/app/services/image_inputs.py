"""绘图请求里图片分片的收集。

字段别名与上游 chatgpt2api / OpenAI 官方 SDK 的口径保持一致：
- 参考图：`image`、`image[]`、`images`、`images[]`
- 遮罩（局部编辑）：`mask`、`mask[]`

不同客户端对「多张图」的编码方式不一样：OpenAI SDK 传 `image[]`，
PicSystem 自己的网页传 `images`，curl / 简单脚本常用重复的 `image`。
全部收下才能做到「用户按官方文档写的调用就能跑」。
"""
from __future__ import annotations

from starlette.datastructures import UploadFile

IMAGE_REFERENCE_FIELDS = ("image", "image[]", "images", "images[]")
MASK_REFERENCE_FIELDS = ("mask", "mask[]")


def collect_uploads(form, fields: tuple[str, ...]) -> list[UploadFile]:
    """按字段别名收集上传分片，保持客户端发送顺序。"""
    uploads: list[UploadFile] = []
    for field in fields:
        for value in form.getlist(field):
            if isinstance(value, UploadFile):
                uploads.append(value)
    return uploads
