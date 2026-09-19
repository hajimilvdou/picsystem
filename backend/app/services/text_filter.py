"""上游文本过滤器：剔除 chatgpt2api 泄露的模型内部标记。

目前处理 `:::writing{...}` 写作指令块：开头标记 `:::writing{...}`、
结尾单独的 `:::`。流式安全——标记可能被 SSE chunk 任意切断，
因此用状态机 + 尾部暂存实现；吞掉标记后紧跟的一个换行也一并吸收
（换行可能与标记分属不同 chunk，用 _skip_lf 跨 feed 追踪）。
仅用于网页对话显示层，/v1 接口不透过滤。
"""
from __future__ import annotations

_OPEN = ":::writing{"  # 块开始标记
_CLOSE = ":::"  # 块结束标记
# 开始标记到 `}` 之间的最大容忍长度，超过视为异常原文放行
_OPEN_MAX = 1000


class WritingMarkerFilter:
    """逐段 feed()，返回可立即输出的干净文本；流结束后调用 flush()。"""

    def __init__(self) -> None:
        self._buf = ""
        self._in_block = False
        self._skip_lf = False

    @staticmethod
    def _held_back(buf: str, marker: str) -> int:
        """buf 尾部与 marker 前缀的最长匹配长度（可能是被切断的标记，需暂存）。"""
        limit = min(len(buf), len(marker) - 1)
        for k in range(limit, 0, -1):
            if buf.endswith(marker[:k]):
                return k
        return 0

    def feed(self, delta: str) -> str:
        self._buf += delta
        out: list[str] = []
        while self._buf:
            if self._skip_lf:
                # 吞掉标记后紧跟的一个换行（可能跨 chunk 到达）
                if self._buf.startswith("\r\n"):
                    self._buf = self._buf[2:]
                    self._skip_lf = False
                elif self._buf.startswith("\n"):
                    self._buf = self._buf[1:]
                    self._skip_lf = False
                elif self._buf == "\r":
                    break  # 等待可能的 \n
                else:
                    self._skip_lf = False
                if not self._buf:
                    break
            if not self._in_block:
                idx = self._buf.find(_OPEN)
                if idx == -1:
                    hold = self._held_back(self._buf, _OPEN)
                    emit = self._buf[: len(self._buf) - hold] if hold else self._buf
                    self._buf = self._buf[len(emit):]
                    out.append(emit)
                    break
                out.append(self._buf[:idx])
                rest = self._buf[idx + len(_OPEN):]
                end = rest.find("}")
                if end == -1:
                    if len(rest) > _OPEN_MAX:
                        # 异常：标记迟迟不闭合，放弃过滤按原文放行
                        out.append(self._buf[idx:])
                        self._buf = ""
                        break
                    self._buf = self._buf[idx:]  # 等待更多数据
                    break
                self._buf = rest[end + 1:]
                self._in_block = True
                self._skip_lf = True
                continue
            # 块内：找结束标记 :::
            idx = self._buf.find(_CLOSE)
            if idx == -1:
                hold = self._held_back(self._buf, _CLOSE)
                emit = self._buf[: len(self._buf) - hold] if hold else self._buf
                self._buf = self._buf[len(emit):]
                out.append(emit)
                break
            out.append(self._buf[:idx])
            self._buf = self._buf[idx + len(_CLOSE):]
            self._in_block = False
            self._skip_lf = True
        return "".join(out)

    def flush(self) -> str:
        """流收尾：剩余暂存按原文放行（未闭合的开始/结束标记不再过滤）。"""
        rest = self._buf
        self._buf = ""
        self._skip_lf = False
        return rest


def strip_writing_markers(text: str) -> str:
    """整段文本的一次性过滤（测试与非流式场景用）。"""
    f = WritingMarkerFilter()
    return f.feed(text) + f.flush()
