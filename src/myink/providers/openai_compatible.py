"""Provider for user-configured OpenAI-compatible chat completion endpoints."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from myink.providers.deepseek import DeepSeekProvider

_MINIMAX_M3 = re.compile(r"(?i)^minimax-m3(?:$|[-_.])")


def is_deepseek_host(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host == "deepseek.com" or host.endswith(".deepseek.com")


def is_minimax_host(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return "minimax" in host


class OpenAICompatibleProvider(DeepSeekProvider):
    def __init__(self, *, api_key: str, base_url: str):
        super().__init__(api_key=api_key, base_url=base_url)
        self._base_url = base_url
        self._keep_thinking_param = is_deepseek_host(base_url)
        self._minimax = is_minimax_host(base_url)

    def name(self) -> str:
        return "openai-compatible"

    def _request_kwargs(self, messages: list[dict], *, model_id: str,
                        max_tokens: int | None, temperature: float | None,
                        json_mode: bool, tools: list[dict] | None,
                        disable_thinking: bool) -> dict:
        kwargs = DeepSeekProvider._request_kwargs(
            messages, model_id=model_id, max_tokens=max_tokens,
            temperature=temperature, json_mode=json_mode, tools=tools,
            disable_thinking=disable_thinking,
        )
        if self._keep_thinking_param:
            return kwargs
        extra: dict = {}
        # inkos #329：能关的模型另说；关不掉或网关会内联 <think> 的，先拆到
        # reasoning_content / reasoning_details，正文只走 content。
        extra["reasoning_split"] = True
        if self._minimax and disable_thinking and _MINIMAX_M3.match(model_id):
            extra["thinking"] = {"type": "disabled"}
        kwargs["extra_body"] = extra
        return kwargs
