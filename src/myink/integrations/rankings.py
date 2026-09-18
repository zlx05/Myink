"""扫榜能力：外部小说榜单经 MCP Client 拉取 → sanitize → 供建书前灵感工具展示。

设计口径（plan.md §10 / 记忆确认）：
- Myink 是 **MCP Client**（接入外部数据源的标准协议）；外部 server 不可信；
- 榜单数据**只当灵感参考展示给用户，不进记忆/事实/事件层**（不落库），
  且**不注入任何生成节点**——扫榜已整体前移至建书前的用户灵感工具；
- **输出 sanitize 防注入**：只留 allowlist 字段、剥控制字符、字段/条数 cap；
- **优雅降级（§6.12）**：`RANKINGS_ENABLED=0` / 网络不可达 / 无匹配工具 / 无有效项
  → 返回内置样例（`source=sample` + `error`），面板照常展示不中断。

入口：`fetch_rankings`（async，供 FastAPI 全局端点直接 await）。进程内 TTL 缓存
（api 与 worker 独立进程各持一份——榜单是全局只读低频公开数据，各进程每小时最多
打一次 MCP，不值得上 Redis 共享缓存）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from myink.config import settings
from myink.integrations.mcp import McpClient, McpError

logger = logging.getLogger(__name__)

# 内置样例（明显虚构，书名带【示例】前缀防模型当真）——网络不可达/被禁用时降级用
_SAMPLE_ITEMS: list[dict[str, Any]] = [
    {"rank": 1, "title": "【示例】九天剑帝", "author": "青竹", "tags": ["仙侠", "无敌流"], "hot": "热榜 1"},
    {"rank": 2, "title": "【示例】万古神域", "author": "墨白", "tags": ["玄幻", "热血"], "hot": "热榜 2"},
    {"rank": 3, "title": "【示例】都市医仙", "author": "南山客", "tags": ["都市", "异能"], "hot": "热榜 3"},
    {"rank": 4, "title": "【示例】星际流浪者", "author": "远行", "tags": ["科幻", "星际"], "hot": "热榜 4"},
    {"rank": 5, "title": "【示例】诡秘档案局", "author": "雾灯", "tags": ["悬疑", "克苏鲁"], "hot": "热榜 5"},
    {"rank": 6, "title": "【示例】重生之商海", "author": "锦鲤", "tags": ["重生", "商战"], "hot": "热榜 6"},
]

# sanitize allowlist：只保留这些键，其余外部字段一律丢弃（防注入）
_ALLOWED = {"rank", "title", "author", "tags", "tag", "hot"}
_FIELD_CAP = {"title": 120, "author": 60, "tags": 12, "hot": 40}
_MAX_TAGS = 8

# 按 source 给榜单工具的建议参数（与 DaoSearch REST 文档对齐；工具名/参数以 list_tools 为准，
# 对不上时 sanitize + 降级兜底）
_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "qidian": {"type": "hotsales", "genre": "overall"},
    "community": {"period": "all-time", "genre": "1"},
}


def _clean_text(value: Any, cap: int) -> str:
    """剥控制字符 + 压缩空白 + cap 截断（外部数据不可信，防注入 prompt）。"""
    if value is None:
        return ""
    s = str(value)
    s = "".join(ch for ch in s if ch >= " " or ch in "\n\t")  # 剥 \x00-\x1f
    s = " ".join(s.split())
    return s[:cap]


def _clean_rank(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _collect_tags(row: dict[str, Any]) -> list[str]:
    tags = row.get("tags")
    if not isinstance(tags, list):
        tags = [row.get("tag")] if row.get("tag") else []
    cleaned: list[str] = []
    for t in tags:
        c = _clean_text(t, _FIELD_CAP["tags"])
        if c and c not in cleaned:
            cleaned.append(c)
        if len(cleaned) >= _MAX_TAGS:
            break
    return cleaned


def _extract_rows(raw: Any) -> list[Any]:
    """宽容解析 MCP 返回：顶层 list / {items|rankings|data|list|books: [...]} / 单条 dict。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("items", "rankings", "data", "list", "books"):
            if isinstance(raw.get(key), list):
                return raw[key]
        if raw.get("title"):
            return [raw]
    return []


def sanitize(raw: Any, *, limit: int) -> list[dict[str, Any]]:
    """外部榜单数据 → 归一 `[{"rank","title","author","tags","hot"}]`（只留 allowlist）。

    输入宽容：已解析 list/dict 或 JSON 文本（McpClient text 块拼平返回的是字符串）都接受；
    非 JSON 文本（工具错误文案等）→ []。未知键丢弃、字段长度 cap、条数 cap 到 limit、
    非 dict 项丢弃、无 title 项丢弃；rank 缺失按序号补。0 有效项 → 返回 []，调用方据此降级样例。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []  # 非 JSON 文本 → 无有效项 → 调用方降级样例
    out: list[dict[str, Any]] = []
    for row in _extract_rows(raw):
        if not isinstance(row, dict):
            continue
        title = _clean_text(row.get("title"), _FIELD_CAP["title"])
        if not title:
            continue
        item: dict[str, Any] = {"title": title, "rank": _clean_rank(row.get("rank")) or len(out) + 1}
        author = _clean_text(row.get("author"), _FIELD_CAP["author"])
        if author:
            item["author"] = author
        hot = row.get("hot")
        if hot is None:
            hot = row.get("heat")
        hot = _clean_text(hot, _FIELD_CAP["hot"])
        if hot:
            item["hot"] = hot
        tags = _collect_tags(row)
        if tags:
            item["tags"] = tags
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _find_tool(tools: list[str], source: str, override: str) -> str | None:
    """榜单工具发现：RANKINGS_TOOL 精确命中优先；否则 name 含 "rank" 模糊匹配，source 命中优先。"""
    if override:
        return override if override in tools else None
    candidates = [t for t in tools if "rank" in t.lower()]
    if not candidates:
        return None
    if source:
        for t in candidates:
            if source in t.lower():
                return t
    return candidates[0]


@dataclass
class RankingsResult:
    source: str = "sample"  # "remote" | "sample"
    tool: str = ""
    fetched_at: str | None = None
    error: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)


def _default_client_factory():
    def factory() -> McpClient:
        return McpClient(settings.rankings_mcp_url, timeout_s=settings.rankings_timeout)

    return factory


class RankingsService:
    """扫榜服务：TTL 缓存 + 降级 + 双入口。测试可注入 settings / client 工厂 / 时钟。"""

    def __init__(self, *, settings_obj=None, client_factory=None, clock=None):
        self._settings = settings_obj or settings
        self._client_factory = client_factory or _default_client_factory()
        self._clock = clock or time.time
        self._cache: dict[str, dict] = {}  # key -> {"ts": float, "result": RankingsResult}

    def _cache_get(self) -> RankingsResult | None:
        entry = self._cache.get("default")
        if not entry:
            return None
        ts, result = entry["ts"], entry["result"]
        if self._clock() - ts < self._settings.rankings_cache_ttl:
            return result
        return None

    async def fetch(self, *, refresh: bool = False) -> RankingsResult:
        st = self._settings
        if not st.rankings_enabled:
            return RankingsResult(source="sample", error="RANKINGS_ENABLED=0 已禁用扫榜", items=_SAMPLE_ITEMS)
        if not refresh:
            cached = self._cache_get()
            if cached is not None:
                return cached
        result = await self._fetch_remote()
        if result.source == "remote":
            self._cache["default"] = {"ts": self._clock(), "result": result}
        return result

    async def _fetch_remote(self) -> RankingsResult:
        st = self._settings
        try:
            async with self._client_factory() as client:
                tools = await client.list_tools()
                tool = _find_tool(tools, st.rankings_source, st.rankings_tool)
                if not tool:
                    return RankingsResult(
                        source="sample",
                        error=f"未在 MCP server 发现榜单工具（可用: {tools[:10] or '无'}）",
                        items=_SAMPLE_ITEMS,
                    )
                raw = await client.call_tool(tool, _TOOL_ARGS.get(st.rankings_source, {}))
                items = sanitize(raw, limit=st.rankings_limit)
                if not items:
                    return RankingsResult(
                        source="sample", error=f"工具 {tool} 返回无有效榜单项", items=_SAMPLE_ITEMS
                    )
                return RankingsResult(
                    source="remote",
                    tool=tool,
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    items=items,
                )
        except asyncio.CancelledError:
            # mcp SDK 内部 cancel scope 触发（初始化/请求挂起被取消）——CancelledError 是
            # BaseException，`except Exception` 捕不住；外部通道不可靠，照常降级不炸节点。
            logger.info("扫榜降级（MCP 请求被取消，连接挂起或通道关闭）")
            return RankingsResult(source="sample", error="扫榜请求被取消", items=_SAMPLE_ITEMS)
        except McpError as exc:
            logger.info("扫榜降级（MCP 错误）: %s", exc)
            return RankingsResult(source="sample", error=str(exc), items=_SAMPLE_ITEMS)
        except Exception as exc:  # noqa: BLE001 - 外部协议不可信，统一降级不炸面板
            logger.exception("扫榜降级（未预期异常）")
            return RankingsResult(source="sample", error=f"扫榜失败: {exc}", items=_SAMPLE_ITEMS)


_service = RankingsService()
_user_services: dict[str, RankingsService] = {}


def _service_for_user(user_id: str) -> RankingsService:
    from myink.environment import rankings_view_for_user

    view = rankings_view_for_user(user_id)
    if view is None:
        return _service
    key = "|".join((
        user_id, view.rankings_mcp_url, view.rankings_source, view.rankings_tool,
        str(view.rankings_enabled), str(view.rankings_timeout), str(view.rankings_limit),
    ))
    cached = _user_services.get(key)
    if cached is not None:
        return cached

    def factory(url=view.rankings_mcp_url, timeout=view.rankings_timeout):
        return McpClient(url, timeout_s=timeout)

    svc = RankingsService(settings_obj=view, client_factory=factory)
    _user_services[key] = svc
    return svc


async def fetch_rankings(refresh: bool = False, user_id: str | None = None) -> dict[str, Any]:
    """async 入口（FastAPI 全局端点，建书前灵感工具；无项目归属，身份由路由断言）。

    已登录且账号环境配置里保存过扫榜项 → 用用户覆盖；否则走进程 .env 默认。
    """
    svc = _service_for_user(user_id) if user_id else _service
    result = await svc.fetch(refresh=refresh)
    return {
        "source": result.source,
        "tool": result.tool,
        "fetched_at": result.fetched_at,
        "error": result.error,
        "items": result.items,
    }
