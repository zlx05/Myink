"""DeepSeek ModelProvider（OpenAI 兼容，§19.3）。

- JSON mode：`response_format={"type": "json_object"}`；
- 磁盘前缀缓存自动命中（同前缀 system 指令），价格差约 50 倍；
- 思考模式通过请求体参数切换（不再靠模型名，旧别名 2026-07-24 废弃）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from openai import OpenAI

from myink.providers.base import ModelProvider, ModelResponse
from myink.providers.think_tag_stripper import (
    LeadingThinkTagStripper,
    isolate_response_body,
    looks_like_unclosed_think,
    strip_leading_think_block,
)

logger = logging.getLogger(__name__)

# 调用层兜底参数（§6.12）：指数退避重试 1s/2s/4s，上限 3 次
MAX_RETRIES = 3
BACKOFF_BASE = [1.0, 2.0, 4.0]


def strip_thinking_text(text: str) -> str:
    """去掉响应开头的完整 think 块（对齐 inkos：正文中间同名标签不动）。"""
    return strip_leading_think_block(text).strip()


def extract_openai_text_part(value) -> str:
    """把 content / reasoning_content / reasoning_details 收成纯文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    content = item.get("content")
                    if isinstance(content, str):
                        parts.append(content)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _message_reasoning(message) -> str:
    return (
        extract_openai_text_part(getattr(message, "reasoning_content", None))
        or extract_openai_text_part(getattr(message, "reasoning_details", None))
    )


def _delta_reasoning(delta) -> str:
    return (
        extract_openai_text_part(getattr(delta, "reasoning_content", None))
        or extract_openai_text_part(getattr(delta, "reasoning_details", None))
    )


class DeepSeekProvider(ModelProvider):
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        # 模块加载不崩溃：key 留空也能 import（generate 时校验）；占位 key 满足 OpenAI client 构造
        self._api_key = api_key or ""
        self._client = OpenAI(
            api_key=self._api_key or "sk-placeholder-not-configured",
            base_url=base_url or "https://api.deepseek.com",
            timeout=120.0,
            max_retries=0,  # 重试由本层控制（可观测、记日志）
        )

    def name(self) -> str:
        return "deepseek"

    @staticmethod
    def _request_kwargs(messages: list[dict], *, model_id: str,
                        max_tokens: int | None, temperature: float | None,
                        json_mode: bool, tools: list[dict] | None,
                        disable_thinking: bool) -> dict:
        kwargs: dict = {"model": model_id, "messages": messages}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if json_mode or tools or disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if tools:
            kwargs["tools"] = tools
        return kwargs

    def generate(self, messages: list[dict], *, model_id: str, max_tokens: int | None = None,
                 temperature: float | None = None, json_mode: bool = False,
                 tools: list[dict] | None = None,
                 disable_thinking: bool = False) -> ModelResponse:
        if not self._api_key:
            return ModelResponse(content="", model_id=model_id, error="未提供 API Key")

        t0 = time.monotonic()
        # v4 思考模式默认开启：JSON / 工具轮 / 正文必须关闭，避免 reasoning 抢输出预算。
        kwargs = self._request_kwargs(
            messages, model_id=model_id, max_tokens=max_tokens,
            temperature=temperature, json_mode=json_mode, tools=tools,
            disable_thinking=disable_thinking,
        )

        last_error: str | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                usage = resp.usage or type("U", (), {})()
                message = resp.choices[0].message
                reasoning = _message_reasoning(message)
                content = isolate_response_body(
                    extract_openai_text_part(message.content),
                    reasoning=reasoning, json_mode=json_mode,
                )
                # inkos：思考与正文分家。JSON 才允许 content 空时借用 reasoning
                #（audit 偶发把 JSON 丢进思考字段）；章节正文永不并入思考。
                # 工具轮 content 空 + tool_calls 非空是正常情况，不触发空白兜底。
                if not content.strip() and not getattr(message, "tool_calls", None):
                    if reasoning.strip() and not json_mode:
                        if attempt < MAX_RETRIES:
                            logger.warning("正文只有思考链（attempt=%d/%d），丢弃 reasoning 后重试",
                                           attempt, MAX_RETRIES)
                            continue
                        return ModelResponse(
                            content="", model_id=model_id,
                            error="模型只返回了思考过程，没有正文",
                            duration_ms=int((time.monotonic() - t0) * 1000))
                    else:
                        if attempt < MAX_RETRIES:
                            logger.warning("DeepSeek 返回空白/空内容（attempt=%d/%d），快速重试",
                                           attempt, MAX_RETRIES)
                            continue
                        return ModelResponse(
                            content="", model_id=model_id,
                            error="DeepSeek 返回空白/空内容（thinking disabled 失效或输出异常）",
                            duration_ms=int((time.monotonic() - t0) * 1000))
                duration_ms = int((time.monotonic() - t0) * 1000)
                tool_calls = None
                if getattr(message, "tool_calls", None):
                    # arguments 是 JSON 字符串（OpenAI 兼容格式），解析失败容错为 {}
                    parsed = []
                    for tc in message.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except (json.JSONDecodeError, TypeError):
                            logger.warning("tool_calls.arguments 解析失败: %r", tc.function.arguments)
                            args = {}
                        parsed.append({"id": tc.id, "name": tc.function.name,
                                       "arguments": args if isinstance(args, dict) else {}})
                    tool_calls = parsed
                return ModelResponse(
                    content=content,
                    model_id=model_id,
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    cache_hit=bool(getattr(usage, "prompt_cache_hit_tokens", 0)),
                    duration_ms=duration_ms,
                    retry_count=attempt,
                    tool_calls=tool_calls,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning("DeepSeek 调用失败(attempt=%d): %s", attempt, last_error)
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE[min(attempt, len(BACKOFF_BASE) - 1)])
        return ModelResponse(content="", model_id=model_id, error=last_error, duration_ms=int((time.monotonic() - t0) * 1000))

    def generate_stream(self, messages: list[dict], *, model_id: str,
                        on_delta: Callable[[str], None],
                        on_reset: Callable[[], None] | None = None,
                        max_tokens: int | None = None,
                        temperature: float | None = None, json_mode: bool = False,
                        tools: list[dict] | None = None,
                        disable_thinking: bool = False) -> ModelResponse:
        """OpenAI 兼容流式调用，最终仍聚合为 ModelResponse 供原校验链使用。"""
        if not self._api_key:
            return ModelResponse(content="", model_id=model_id, error="未提供 API Key")

        t0 = time.monotonic()
        kwargs = self._request_kwargs(
            messages, model_id=model_id, max_tokens=max_tokens,
            temperature=temperature, json_mode=json_mode, tools=tools,
            disable_thinking=disable_thinking,
        )
        kwargs["stream"] = True
        # OpenAI 兼容协议在最后一个空 choices 帧返回完整 usage。
        kwargs["stream_options"] = {"include_usage": True}
        last_error: str | None = None

        for attempt in range(MAX_RETRIES + 1):
            emitted_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_parts: dict[int, dict[str, str]] = {}
            usage = None
            emitted = False
            stripper = LeadingThinkTagStripper()
            try:
                stream = self._client.chat.completions.create(**kwargs)
                for chunk in stream:
                    if getattr(chunk, "usage", None) is not None:
                        usage = chunk.usage
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = chunk.choices[0].delta
                    text = extract_openai_text_part(getattr(delta, "content", None))
                    if text:
                        emittable = stripper.push(text)
                        if emittable:
                            emitted_parts.append(emittable)
                            on_delta(emittable)
                            emitted = True
                    reasoning = _delta_reasoning(delta)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    for tc in (getattr(delta, "tool_calls", None) or []):
                        idx = int(getattr(tc, "index", 0) or 0)
                        row = tool_parts.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if getattr(tc, "id", None):
                            row["id"] += tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            row["name"] += getattr(fn, "name", None) or ""
                            row["arguments"] += getattr(fn, "arguments", None) or ""

                leftover = stripper.flush()
                if leftover and not looks_like_unclosed_think(leftover):
                    emitted_parts.append(leftover)
                streamed = "".join(emitted_parts)
                reasoning = "".join(reasoning_parts)
                content = isolate_response_body(
                    streamed, reasoning=reasoning, json_mode=json_mode,
                )
                if content and not streamed.strip():
                    on_delta(content)
                    emitted = True
                tool_calls: list[dict] | None = None
                if tool_parts:
                    parsed_tools: list[dict] = []
                    for idx in sorted(tool_parts):
                        row = tool_parts[idx]
                        try:
                            args = json.loads(row["arguments"] or "{}")
                        except (json.JSONDecodeError, TypeError):
                            logger.warning("流式 tool_calls.arguments 解析失败: %r", row["arguments"])
                            args = {}
                        parsed_tools.append({
                            "id": row["id"], "name": row["name"],
                            "arguments": args if isinstance(args, dict) else {},
                        })
                    tool_calls = parsed_tools

                if not content.strip() and not tool_calls:
                    if reasoning.strip() and not json_mode:
                        if attempt < MAX_RETRIES:
                            if emitted and on_reset:
                                on_reset()
                            logger.warning("流式正文只有思考链（attempt=%d/%d），丢弃后重试",
                                           attempt, MAX_RETRIES)
                            continue
                        return ModelResponse(
                            content="", model_id=model_id,
                            error="模型只返回了思考过程，没有正文",
                            duration_ms=int((time.monotonic() - t0) * 1000),
                        )
                    elif attempt < MAX_RETRIES:
                        if emitted and on_reset:
                            on_reset()
                        logger.warning("DeepSeek 流式返回空内容（attempt=%d/%d），快速重试",
                                       attempt, MAX_RETRIES)
                        continue
                    else:
                        return ModelResponse(
                            content="", model_id=model_id,
                            error="DeepSeek 流式返回空白/空内容",
                            duration_ms=int((time.monotonic() - t0) * 1000),
                        )
                usage = usage or type("U", (), {})()
                return ModelResponse(
                    content=content,
                    model_id=model_id,
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    cache_hit=bool(getattr(usage, "prompt_cache_hit_tokens", 0)),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    retry_count=attempt,
                    tool_calls=tool_calls,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning("DeepSeek 流式调用失败(attempt=%d): %s", attempt, last_error)
                if emitted and on_reset:
                    on_reset()
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE[min(attempt, len(BACKOFF_BASE) - 1)])
        return ModelResponse(
            content="", model_id=model_id, error=last_error,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
