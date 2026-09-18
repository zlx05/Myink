"""剥离响应起始处的完整 think 块（对齐 inkos issue #329）。

部分 OpenAI 兼容服务（MiniMax M2.x、经网关代理的 DeepSeek-R1 类模型）会把
思考内容以 <think> 标签内联在 content 开头。这里只剥「起始处的完整 think 块」：
正文中间出现的同名标签不动；起始处未闭合的块也不剥（避免正文根本没生成时丢数据）。

流式剥离器在能确定「开头不是 think 块」之前先缓冲，思考内容不会先展示再消失。
"""

from __future__ import annotations

# 常见围栏。inkos 主路径是 <think>；另兼容 thinking / reasoning / MiniMax mm:think。
_TAG_PAIRS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
    ("<mm:think>", "</mm:think>"),
)


def _open_tag_match(rest: str) -> tuple[str, str] | None:
    lowered = rest.lower()
    for open_tag, close_tag in _TAG_PAIRS:
        if lowered.startswith(open_tag):
            return open_tag, close_tag
    return None


def _maybe_open_tag_prefix(rest: str) -> bool:
    lowered = rest.lower()
    return any(open_tag.startswith(lowered) for open_tag, _ in _TAG_PAIRS)


def looks_like_unclosed_think(text: str) -> bool:
    """起始处像未闭合的 think 围栏（flush 出来的残留，写作路径应丢弃）。"""
    rest = text.lstrip()
    return _open_tag_match(rest) is not None


class LeadingThinkTagStripper:
    """流式剥离器：只处理响应起始处的完整 think 块。"""

    def __init__(self) -> None:
        self._state = "detecting"
        self._pending = ""
        self._close = ""

    def push(self, chunk: str) -> str:
        if self._state == "passthrough":
            return chunk
        self._pending += chunk

        if self._state == "detecting":
            rest = self._pending.lstrip()
            if not rest:
                return ""
            matched = _open_tag_match(rest)
            if matched is None:
                if _maybe_open_tag_prefix(rest):
                    return ""
                self._state = "passthrough"
                out = self._pending
                self._pending = ""
                return out
            self._state = "inside"
            self._close = matched[1]

        close_index = self._pending.lower().find(self._close)
        if close_index < 0:
            return ""
        after = self._pending[close_index + len(self._close):].lstrip()
        self._pending = ""
        self._state = "passthrough"
        return after

    def flush(self) -> str:
        out = self._pending
        self._pending = ""
        self._state = "passthrough"
        return out


def strip_leading_think_block(text: str) -> str:
    """非流式：剥离字符串起始处的完整 think 块（语义与流式剥离器一致）。"""
    stripper = LeadingThinkTagStripper()
    return stripper.push(text) + stripper.flush()


def isolate_response_body(content: str, *, reasoning: str = "", json_mode: bool = False) -> str:
    """inkos 规则：正文只用 content；思考字段永不并入章节。

    JSON 结构化输出在 content 全空时才借用 reasoning（audit 偶发把 JSON 放进思考字段），
    且仍先剥开头 think 块。纯文本正文即使思考开着也不用 reasoning。
    """
    body = strip_leading_think_block(content).strip()
    if body:
        return body
    if json_mode and reasoning.strip():
        return strip_leading_think_block(reasoning).strip()
    return ""
