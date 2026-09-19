"""图库标签的规范化与查询条件。

存储约定：`stored_files.tags` 为**逗号分隔**字符串（如 `风景,猫,4k`）。
因此标签内不允许出现逗号、空白与 LIKE 通配符——`normalize_tags` 会先清洗再落库，
`tag_filter` 会转义通配符后用四种位置模式做精确匹配，避免 `a_b` 这种标签被 `_` 通配误匹配。
"""
from __future__ import annotations

import re

MAX_TAGS_PER_FILE = 10
MAX_TAG_LENGTH = 24

# 保留：字母、数字、下划线、连字符、任意语言文字（中文等）；其余一律剔除
_TAG_CLEAN = re.compile(r"[^\w\-]+", re.UNICODE)
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def normalize_tags(raw: object) -> list[str]:
    """清洗标签列表：去重、去非法字符、限长限数。非列表入参按空处理。"""
    if not isinstance(raw, (list, tuple, set)):
        return []
    tags: list[str] = []
    for item in raw:
        text = _TAG_CLEAN.sub("", str(item or "").strip())[:MAX_TAG_LENGTH]
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= MAX_TAGS_PER_FILE:
            break
    return tags


def tags_to_column(tags: list[str]) -> str:
    return ",".join(tags)


def column_to_tags(value: object) -> list[str]:
    return [item for item in str(value or "").split(",") if item]


def tag_filter(column, tag: str):
    """返回「该列包含指定标签」的 SQLAlchemy 条件（双方言通用，无需字符串切分函数）。"""
    safe = str(tag or "").translate(_LIKE_ESCAPE)
    if not safe:
        return None
    return (
        (column == tag)
        | column.like(f"{safe},%", escape="\\")
        | column.like(f"%,{safe}", escape="\\")
        | column.like(f"%,{safe},%", escape="\\")
    )
