"""MCP 客户端薄封装（plan.md §10：Myink 是 MCP Client，不是 Server）。

MCP = 接入外部工具的标准协议。外部 server 不可信：本模块只负责协议层
（连接 / initialize / list_tools / call_tool），不暴露给 LLM agent 自由调用——
调用方（编排层受控原子能力，见 rankings.py）拿到原始输出后必须 sanitize。

**所有 mcp SDK 的 import 都收敛在本文件**：锁 `mcp~=1.29`（v2 有破坏性变更：
snake_case 字段 / `call_tool(name, arguments)` / httpx2），将来升 2.x 只改这里。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

# 锁 1.x：`streamablehttp_client` 是 1.x 函数名（个别版本还提供别名），v2 移除。
try:
    from mcp.client.streamable_http import streamablehttp_client as _streamablehttp_client
except ImportError:  # pragma: no cover - 版本兼容兜底
    from mcp.client.streamable_http import streamable_http_client as _streamablehttp_client  # type: ignore

from mcp import ClientSession


class McpError(Exception):
    """MCP 协议/网络/工具错误统一异常。调用方据此降级（§6.12），不中断批次。"""


def _truncate(value: Any, cap: int = 200) -> str:
    text = str(value)
    return text if len(text) <= cap else text[:cap] + "…"


class McpClient:
    """MCP Client 薄封装：async 上下文管理器，测试可注入 session 工厂。

    用法：`async with McpClient(url, timeout_s=10) as client: text = await client.call_tool(...)`
    `session_factory` 供测试注入 stub 会话（鸭子类型：initialize/list_tools/call_tool）。
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = 10,
        headers: dict[str, str] | None = None,
        session_factory: Any = None,
    ):
        self._url = url
        self._timeout = timeout_s
        self._headers = headers or {}
        self._session_factory = session_factory  # async () -> ClientSession，测试注入
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpClient":
        if self._session_factory is not None:
            self._session = await self._session_factory()
        else:
            read, write, _ = await self._stack.enter_async_context(
                _streamablehttp_client(self._url, headers=self._headers, timeout=self._timeout)
            )
            self._session = ClientSession(read, write)
        await self._stack.enter_async_context(self._session)
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self._stack.aclose()
        except (RuntimeError, asyncio.CancelledError):
            # mcp SDK 1.x + anyio 取消竞态：initialize 中途被内部 cancel scope 取消时
            # aclose() 报 "asynchronous generator is already running"——外部协议不可信，
            # 释放资源失败不向上抛（调用方按降级处理），重置栈防复用污染。
            self._stack = AsyncExitStack()
        self._session = None

    async def list_tools(self) -> list[str]:
        """返回 server 可用工具名列表（无工具 → []）。"""
        if self._session is None:
            raise McpError("McpClient 未连接（需先 async with 进入）")
        try:
            result = await self._session.list_tools()
        except Exception as exc:  # noqa: BLE001 - 外部协议不可信，统一归一
            raise McpError(f"list_tools 失败: {_truncate(exc)}") from exc
        return [t.name for t in (result.tools or [])]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用工具并返回拼平的 text；is_error / 无文本 → McpError。"""
        if self._session is None:
            raise McpError("McpClient 未连接（需先 async with 进入）")
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001
            raise McpError(f"call_tool({name}) 失败: {_truncate(exc)}") from exc
        return self._flatten_content(result)

    @staticmethod
    def _flatten_content(result: Any) -> str:
        """从 CallToolResult 取文本：text 块拼平 → 回退 structuredContent → 抛错。

        兼容 v1（isError/structuredContent）与 v2（is_error/structured_content）命名，
        降 v2 时本文件是唯一需改的点。
        """
        is_error = getattr(result, "isError", None) or getattr(result, "is_error", None)
        if is_error:
            raise McpError(f"工具返回错误: {_truncate(getattr(result, 'content', None))}")
        text_parts = [
            block.text
            for block in (getattr(result, "content", None) or [])
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        text = "\n".join(text_parts).strip()
        if text:
            return text
        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)
        raise McpError("工具返回为空（无 text 块，也无 structuredContent）")
