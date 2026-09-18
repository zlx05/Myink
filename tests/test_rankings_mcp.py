"""扫榜灵感模块测试（plan.md §10：MCP Client → sanitize → 建书前灵感工具 → 降级）。全离线：stub/注入，不触外网。

覆盖：
- McpClient：text 块拼平 / is_error / 无文本 / structuredContent 回退 / list_tools 归一（stub 会话鸭子类型）；
- sanitize：allowlist / 剥控制字符 / 字段与条数 cap / 非 dict / 无 title 丢弃 / JSON 文本输入（McpClient 返回的是字符串）；
- RankingsService：禁用 / TTL 缓存命中与过期 / refresh 绕过 / McpError 降级 / 无工具降级 / 无有效项降级 / remote 归一；
- facade fetch_rankings：无 pid 全局入口形状（source/tool/fetched_at/error/items）；
- API：全局 /internal/v1/rankings 端点 → 200 形状（RankingsOut）+ refresh 透传；缺失身份 → 403 fail closed。
  扫榜已整体前移至建书前——不注入任何生成节点（plan_messages / 图节点不再拉榜单）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from myink.api.main import app
from myink.db import new_session
from myink.integrations import fetch_rankings as facade_fetch_rankings
from myink.integrations.mcp import McpClient, McpError
from myink.integrations.rankings import (
    RankingsService,
    _SAMPLE_ITEMS,
    _find_tool,
    sanitize,
)
from myink.models import User

client = TestClient(app)

# ---- 测试 stub ----


@dataclass
class FakeSettings:
    """RankingsService 注入的最小 settings（只用得到这些字段）。"""

    rankings_enabled: bool = True
    rankings_cache_ttl: float = 3600
    rankings_limit: int = 10
    rankings_source: str = "qidian"
    rankings_tool: str = ""


class FakeClient:
    """async 上下文管理器，鸭子类型 McpClient（client_factory 注入）。"""

    def __init__(self, tools: list[str], call_result: object):
        self.tools = tools
        self.call_result = call_result
        self.list_calls = 0
        self.tool_calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_tools(self):
        self.list_calls += 1
        return self.tools

    async def call_tool(self, name, arguments):
        self.tool_calls.append((name, arguments))
        if isinstance(self.call_result, BaseException):
            raise self.call_result
        return self.call_result


def _make_clock():
    t = [0.0]
    return (lambda: t[0]), t


def _service(fake, *, enabled=True, ttl=3600, limit=10, source="qidian", tool="", clock=None):
    st = FakeSettings(enabled, ttl, limit, source, tool)
    svc = RankingsService(settings_obj=st, client_factory=lambda: fake, clock=clock or (lambda: 0.0))
    return svc, st, fake


class StubSession:
    """鸭子类型 mcp ClientSession：async 上下文管理 + initialize/list_tools/call_tool。"""

    def __init__(self, tools=None, call_result=None, call_exc=None):
        self.tools = tools or []
        self.call_result = call_result
        self.call_exc = call_exc
        self.initialized = False
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=t) for t in self.tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.call_exc:
            raise self.call_exc
        return self.call_result


def _stub_client(session):
    async def factory():
        return session

    return McpClient("http://fake", timeout_s=1, session_factory=factory)


# ---- McpClient（stub 会话鸭子类型）----


def test_mcp_call_tool_flattens_text_blocks_and_initializes():
    session = StubSession(
        call_result=SimpleNamespace(
            isError=False,
            content=[
                SimpleNamespace(type="text", text="line1"),
                SimpleNamespace(type="text", text=""),
                SimpleNamespace(type="text", text="line2"),
            ],
        ),
    )

    async def go():
        async with _stub_client(session) as c:
            text = await c.call_tool("rank", {})
            tools = await c.list_tools()
            return text, tools

    text, tools = asyncio.run(go())
    assert session.initialized, "__aenter__ 应完成 initialize() 握手"
    assert session.calls == [("rank", {})]
    assert text == "line1\nline2", "text 块拼平、空块跳过"
    assert tools == []


def test_mcp_list_tools_normalizes_names():
    session = StubSession(tools=["qidian_rank", "web_search"])

    async def go():
        async with _stub_client(session) as c:
            return await c.list_tools()

    assert asyncio.run(go()) == ["qidian_rank", "web_search"]


def test_mcp_call_tool_is_error_raises():
    session = StubSession(call_result=SimpleNamespace(
        isError=True, content=[SimpleNamespace(type="text", text="boom")]))

    async def go():
        async with _stub_client(session) as c:
            await c.call_tool("rank", {})

    with pytest.raises(McpError, match="工具返回错误"):
        asyncio.run(go())


def test_mcp_call_tool_no_text_raises():
    session = StubSession(call_result=SimpleNamespace(isError=False, content=[]))

    async def go():
        async with _stub_client(session) as c:
            await c.call_tool("rank", {})

    with pytest.raises(McpError, match="为空"):
        asyncio.run(go())


def test_mcp_call_tool_structured_content_fallback():
    session = StubSession(call_result=SimpleNamespace(
        isError=False, content=[], structuredContent={"items": [1, 2]}))

    async def go():
        async with _stub_client(session) as c:
            return await c.call_tool("rank", {})

    assert asyncio.run(go()) == json.dumps({"items": [1, 2]}, ensure_ascii=False)


# ---- sanitize（allowlist / 剥控制字符 / cap / 丢弃）----


def test_sanitize_allowlist_and_normalizes():
    raw = {
        "items": [
            {"rank": 1, "title": "甲", "author": "张三", "tags": ["仙侠", "无敌"], "hot": "1.2万",
             "evil_key": "注入"},
            {"rank": "2", "title": "  乙\n乙  ", "author": None, "tag": "玄幻"},
            {"not_title": True},
        ]
    }
    items = sanitize(raw, limit=10)
    assert items[0] == {"rank": 1, "title": "甲", "author": "张三", "tags": ["仙侠", "无敌"], "hot": "1.2万"}
    assert "evil_key" not in items[0], "未知键丢弃（防注入）"
    assert items[1]["title"] == "乙 乙", "剥控制字符 + 压缩空白"
    assert items[1]["tags"] == ["玄幻"], "tags 缺失回退单 tag"
    assert len(items) == 2, "无 title 项丢弃"


def test_sanitize_accepts_json_string_input():
    """McpClient.call_tool 返回的是 text 块拼平的 JSON 字符串 → sanitize 必须能解析。"""
    raw = '[{"rank":1,"title":"甲","tags":["仙侠"],"evil":"x"},{"rank":2,"title":"乙"}]'
    items = sanitize(raw, limit=10)
    assert items == [{"rank": 1, "title": "甲", "tags": ["仙侠"]}, {"rank": 2, "title": "乙"}]


def test_sanitize_non_json_text_returns_empty():
    assert sanitize("工具错误文案：服务器内部错误", limit=10) == []
    assert sanitize(12345, limit=10) == []


def test_sanitize_control_chars_stripped():
    raw = [{"rank": 1, "title": "甲\x00\x1f乙\r\n", "author": "张\x07三", "hot": "12\x01万"}]
    items = sanitize(raw, limit=10)
    assert items[0]["title"] == "甲乙", "\x00/\x1f/\r 剥掉，\n 随后被 split 折叠"
    assert items[0]["author"] == "张三"
    assert items[0]["hot"] == "12万"


def test_sanitize_limit_caps():
    raw = [{"rank": i, "title": f"书{i}"} for i in range(1, 20)]
    items = sanitize(raw, limit=5)
    assert len(items) == 5
    assert items[-1]["rank"] == 5


def test_sanitize_missing_rank_backfills_by_position():
    raw = [{"title": "甲"}, {"rank": 99, "title": "乙"}]
    items = sanitize(raw, limit=10)
    assert [i["rank"] for i in items] == [1, 99], "缺 rank 按出现顺序补"


# ---- _find_tool ----


def test_find_tool_rank_substring_and_source_priority():
    tools = ["qidian_rank", "community_rank"]
    assert _find_tool(tools, "qidian", "") == "qidian_rank"
    assert _find_tool(tools, "community", "") == "community_rank"
    assert _find_tool(tools, "other", "") == "qidian_rank", "无 source 命中 → 取首个 rank 工具"
    assert _find_tool(["qidian_rank", "community_rank"], "qidian", "community_rank") == "community_rank", \
        "override 精确命中优先"
    assert _find_tool(["web_search"], "qidian", "") is None, "无 rank 工具 → None（降级）"
    assert _find_tool(["qidian_rank"], "qidian", "missing") is None, "override 不匹配 → None"


# ---- RankingsService：禁用 / 缓存 / 降级 / remote ----


def test_service_disabled_returns_sample_without_mcp():
    fake = FakeClient([], "[]")
    svc, st, _ = _service(fake, enabled=False)
    result = asyncio.run(svc.fetch())
    assert result.source == "sample"
    assert result.error == "RANKINGS_ENABLED=0 已禁用扫榜"
    assert result.items == _SAMPLE_ITEMS
    assert fake.list_calls == 0, "禁用不触 MCP"


def test_service_remote_returns_sanitized_items():
    fake = FakeClient(["qidian_rank"], json.dumps([
        {"rank": 1, "title": "甲", "author": "张", "tags": ["仙侠"], "hot": "1.2万", "evil": "x"},
        {"rank": 2, "title": "乙"},
    ]))
    svc, st, _ = _service(fake)
    result = asyncio.run(svc.fetch())
    assert result.source == "remote"
    assert result.tool == "qidian_rank"
    assert result.fetched_at is not None
    assert result.items == [
        {"rank": 1, "title": "甲", "author": "张", "tags": ["仙侠"], "hot": "1.2万"},
        {"rank": 2, "title": "乙"},
    ]
    assert fake.tool_calls == [("qidian_rank", {"type": "hotsales", "genre": "overall"})]


def test_service_caches_within_ttl_and_refresh_bypasses():
    fake = FakeClient(["qidian_rank"], json.dumps([{"rank": 1, "title": "甲"}]))
    clock, t = _make_clock()
    svc, st, _ = _service(fake, clock=clock)

    r1 = asyncio.run(svc.fetch())
    assert r1.source == "remote"
    assert fake.list_calls == 1
    # TTL 内第二次不重打 MCP
    asyncio.run(svc.fetch())
    assert fake.list_calls == 1, "TTL 内缓存命中，不重复打 MCP"

    asyncio.run(svc.fetch(refresh=True))
    assert fake.list_calls == 2, "refresh=True 绕过缓存重拉"


def test_service_ttl_expiry_refetches():
    fake = FakeClient(["qidian_rank"], json.dumps([{"rank": 1, "title": "甲"}]))
    clock, t = _make_clock()
    svc, st, _ = _service(fake, ttl=100, clock=clock)

    asyncio.run(svc.fetch())
    assert fake.list_calls == 1
    t[0] = 100.0  # TTL 到点
    asyncio.run(svc.fetch())
    assert fake.list_calls == 2, "TTL 过期重新拉取"


def test_service_mcp_error_degrades_to_sample():
    fake = FakeClient(["qidian_rank"], McpError("连接超时"))
    svc, st, _ = _service(fake)
    result = asyncio.run(svc.fetch())
    assert result.source == "sample"
    assert result.items == _SAMPLE_ITEMS
    assert "连接超时" in (result.error or "")


def test_service_unexpected_error_degrades_to_sample():
    fake = FakeClient(["qidian_rank"], RuntimeError("神秘错误"))
    svc, st, _ = _service(fake)
    result = asyncio.run(svc.fetch())
    assert result.source == "sample"
    assert "神秘错误" in (result.error or "")


def test_service_cancelled_error_degrades_to_sample():
    # mcp SDK 内部 cancel scope（连接挂起/通道关闭）抛 CancelledError——BaseException，
    # 不是 Exception，`except Exception` 捕不住；不加这条会直接炸图节点（真实 bug）。
    fake = FakeClient(["qidian_rank"], asyncio.CancelledError("cancelled"))
    svc, st, _ = _service(fake)
    result = asyncio.run(svc.fetch())
    assert result.source == "sample"
    assert result.items == _SAMPLE_ITEMS
    assert "取消" in (result.error or "")


def test_service_no_tool_found_degrades_to_sample():
    fake = FakeClient(["web_search"], "[]")
    svc, st, _ = _service(fake)
    result = asyncio.run(svc.fetch())
    assert result.source == "sample"
    assert "未在 MCP server 发现榜单工具" in (result.error or "")
    assert fake.tool_calls == [], "无工具不 call_tool"


def test_service_no_valid_items_degrades_to_sample():
    fake = FakeClient(["qidian_rank"], json.dumps([{"foo": 1}]))
    svc, st, _ = _service(fake)
    result = asyncio.run(svc.fetch())
    assert result.source == "sample"
    assert "无有效榜单项" in (result.error or "")


# ---- facade fetch_rankings（全局无 pid 入口）----


def test_facade_fetch_rankings_shape(monkeypatch):
    fake = FakeClient(["qidian_rank"], json.dumps([{"rank": 1, "title": "甲"}]))
    svc, st, _ = _service(fake)
    monkeypatch.setattr("myink.integrations.rankings._service", svc)
    result = asyncio.run(facade_fetch_rankings())
    assert set(result) == {"source", "tool", "fetched_at", "error", "items"}
    assert result["source"] == "remote"
    assert result["items"][0]["title"] == "甲"


# ---- API 端点（TestClient + 身份头 / 越权矩阵）----


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `myink init`（demo 用户未建）"
        return u.id


def _h(uid: str | uuid.UUID | None) -> dict:
    """请求头：X-Myink-User = 网关已验证的 JWT sub（None → 不带，测 fail closed）。"""
    return {"X-Myink-User": str(uid)} if uid is not None else {}


def test_rankings_endpoint_200_shape(monkeypatch):
    async def fake(refresh=False, user_id=None):
        return {"source": "remote", "tool": "qidian_rank",
                "fetched_at": "2026-01-01T00:00:00+00:00", "error": None,
                "items": [{"rank": 1, "title": "甲", "author": "张", "tags": ["仙侠"], "hot": "1.2万"}]}

    monkeypatch.setattr("myink.api.routes_rankings.fetch_rankings", fake)
    resp = client.get("/internal/v1/rankings", headers=_h(_demo_user_id()))
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "remote"
    assert body["items"][0] == {"rank": 1, "title": "甲", "author": "张", "tags": ["仙侠"], "hot": "1.2万"}


def test_rankings_endpoint_refresh_param_passed(monkeypatch):
    seen: list[bool] = []

    async def fake(refresh=False, user_id=None):
        seen.append(refresh)
        return {"source": "sample", "tool": "", "fetched_at": None, "error": "已禁用", "items": []}

    monkeypatch.setattr("myink.api.routes_rankings.fetch_rankings", fake)
    resp = client.get("/internal/v1/rankings?refresh=true", headers=_h(_demo_user_id()))
    assert resp.status_code == 200
    assert seen == [True]


def test_rankings_endpoint_ownership():
    """身份断言（current_user fail closed，全局端点无项目归属）：缺失身份 → 403。"""
    assert client.get("/internal/v1/rankings").status_code == 403
