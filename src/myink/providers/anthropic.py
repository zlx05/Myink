"""Anthropic Messages API provider with text and tool-call streaming support."""

from __future__ import annotations

import json
import logging
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from myink.providers.base import ModelProvider, ModelResponse
from myink.providers.think_tag_stripper import (
    LeadingThinkTagStripper,
    isolate_response_body,
    looks_like_unclosed_think,
)

logger = logging.getLogger(__name__)
MAX_RETRIES = 3
BACKOFF_BASE = [1.0, 2.0, 4.0]


def _endpoint_url(base_url: str, endpoint: str) -> str:
    """解析出 {base}/v1/{endpoint}（容忍 base_url 已带 /v1 或 /v1/messages）。"""
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/messages"):
        path = path[: -len("/messages")]
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/{endpoint}", "", ""))


def _messages_url(base_url: str) -> str:
    return _endpoint_url(base_url, "messages")


def _models_url(base_url: str) -> str:
    return _endpoint_url(base_url, "models")


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    converted = []
    for item in tools:
        fn = item.get("function", item)
        converted.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return converted


def _convert_messages(messages: list[dict], json_mode: bool) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    converted: list[dict] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "system":
            system_parts.append(str(message.get("content", "")))
            continue
        if role == "tool":
            converted.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": str(message.get("content", "")),
                }],
            })
            continue
        content: str | list[dict] = str(message.get("content", ""))
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in tool_calls:
                fn = call.get("function", {})
                arguments = fn.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                blocks.append({
                    "type": "tool_use", "id": call.get("id", ""),
                    "name": fn.get("name", ""), "input": arguments,
                })
            content = blocks
        converted.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    if json_mode:
        system_parts.append("Return exactly one valid JSON value. Do not use Markdown fences or commentary.")
    return "\n\n".join(part for part in system_parts if part), converted


class AnthropicProvider(ModelProvider):
    def __init__(self, *, api_key: str, base_url: str):
        self._api_key = api_key
        self._url = _messages_url(base_url)
        self._client = httpx.Client(timeout=120.0)

    def name(self) -> str:
        return "anthropic"

    def _payload(self, messages: list[dict], *, model_id: str, max_tokens: int | None,
                 temperature: float | None, json_mode: bool,
                 tools: list[dict] | None, stream: bool) -> dict:
        system, converted = _convert_messages(messages, json_mode)
        payload: dict = {
            "model": model_id,
            "messages": converted,
            "max_tokens": max_tokens or 8192,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        converted_tools = _convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def generate(self, messages: list[dict], *, model_id: str, max_tokens: int | None = None,
                 temperature: float | None = None, json_mode: bool = False,
                 tools: list[dict] | None = None,
                 disable_thinking: bool = False) -> ModelResponse:
        if not self._api_key:
            return ModelResponse(content="", model_id=model_id, error="Anthropic API Key 未配置")
        t0 = time.monotonic()
        payload = self._payload(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode,
                                tools=tools, stream=False)
        last_error: str | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._client.post(self._url, headers=self._headers(), json=payload)
                response.raise_for_status()
                data = response.json()
                content = isolate_response_body("".join(
                    block.get("text", "") for block in data.get("content", [])
                    if block.get("type") == "text"
                ))
                calls = [{"id": block.get("id", ""), "name": block.get("name", ""),
                          "arguments": block.get("input", {})}
                         for block in data.get("content", []) if block.get("type") == "tool_use"] or None
                usage = data.get("usage", {})
                if not content.strip() and not calls:
                    return ModelResponse(
                        content="", model_id=model_id, error="Anthropic 返回空白/空内容",
                        duration_ms=int((time.monotonic() - t0) * 1000), retry_count=attempt,
                    )
                return ModelResponse(
                    content=content, model_id=model_id,
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    retry_count=attempt, tool_calls=calls,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Anthropic 调用失败(attempt=%d): %s", attempt, last_error)
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE[min(attempt, len(BACKOFF_BASE) - 1)])
        return ModelResponse(content="", model_id=model_id, error=last_error,
                             duration_ms=int((time.monotonic() - t0) * 1000))

    def generate_stream(self, messages: list[dict], *, model_id: str,
                        on_delta: Callable[[str], None],
                        on_reset: Callable[[], None] | None = None,
                        max_tokens: int | None = None,
                        temperature: float | None = None, json_mode: bool = False,
                        tools: list[dict] | None = None,
                        disable_thinking: bool = False) -> ModelResponse:
        if not self._api_key:
            return ModelResponse(content="", model_id=model_id, error="Anthropic API Key 未配置")
        t0 = time.monotonic()
        payload = self._payload(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode,
                                tools=tools, stream=True)
        last_error: str | None = None
        for attempt in range(MAX_RETRIES + 1):
            parts: list[str] = []
            tool_parts: dict[int, dict] = {}
            input_tokens = 0
            output_tokens = 0
            emitted = False
            stripper = LeadingThinkTagStripper()
            try:
                with self._client.stream("POST", self._url, headers=self._headers(), json=payload) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw == "[DONE]":
                            continue
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind == "error":
                            detail = event.get("error", {})
                            raise RuntimeError(str(detail.get("message") or "Anthropic 流式响应错误"))
                        if kind == "message_start":
                            input_tokens = int(event.get("message", {}).get("usage", {}).get("input_tokens", 0) or 0)
                        elif kind == "content_block_start":
                            block = event.get("content_block", {})
                            if block.get("type") == "tool_use":
                                tool_parts[int(event.get("index", 0))] = {
                                    "id": block.get("id", ""), "name": block.get("name", ""),
                                    "arguments": "",
                                }
                        elif kind == "content_block_delta":
                            index = int(event.get("index", 0))
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    emittable = stripper.push(text)
                                    if emittable:
                                        parts.append(emittable)
                                        on_delta(emittable)
                                        emitted = True
                            elif delta.get("type") == "input_json_delta" and index in tool_parts:
                                tool_parts[index]["arguments"] += delta.get("partial_json", "")
                        elif kind == "message_delta":
                            output_tokens = int(event.get("usage", {}).get("output_tokens", output_tokens) or 0)
                leftover = stripper.flush()
                if leftover and not looks_like_unclosed_think(leftover):
                    parts.append(leftover)
                content = isolate_response_body("".join(parts))
                calls = []
                for index in sorted(tool_parts):
                    row = tool_parts[index]
                    try:
                        arguments = json.loads(row["arguments"] or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    calls.append({"id": row["id"], "name": row["name"], "arguments": arguments})
                if not content.strip() and not calls:
                    return ModelResponse(
                        content="", model_id=model_id, error="Anthropic 流式返回空白/空内容",
                        duration_ms=int((time.monotonic() - t0) * 1000), retry_count=attempt,
                    )
                return ModelResponse(
                    content=content, model_id=model_id,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    duration_ms=int((time.monotonic() - t0) * 1000), retry_count=attempt,
                    tool_calls=calls or None,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Anthropic 流式调用失败(attempt=%d): %s", attempt, last_error)
                if emitted and on_reset:
                    on_reset()
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE[min(attempt, len(BACKOFF_BASE) - 1)])
        return ModelResponse(content="", model_id=model_id, error=last_error,
                             duration_ms=int((time.monotonic() - t0) * 1000))
