"""模型连接探针（providers/probe.py）回归：列表拉取 / 联通测试 / 错误脱敏。

httpx 用 MockTransport 打桩（同 tests/test_custom_providers.py 的范式），不触网。
"""

from __future__ import annotations

import httpx

from myink.providers import probe


def _patch_client(monkeypatch, handler):
    """把 probe 内构造的 httpx.Client 换成走 MockTransport 的等价 client。"""
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(probe.httpx, "Client", factory)


# ---- 列表拉取 ----

def test_list_models_openai_builds_models_url_and_dedupes(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "pro"}, {"id": "flash"}, {"id": "pro"}]})

    _patch_client(monkeypatch, handler)
    models, error = probe.list_models("openai", "https://api.example.com/v1", "sk-test")

    assert error is None
    assert models == ["flash", "pro"]  # 去重 + 排序
    assert seen["url"] == "https://api.example.com/v1/models"
    assert seen["auth"] == "Bearer sk-test"


def test_list_models_anthropic_uses_v1_models_and_api_key_header(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5"}]})

    _patch_client(monkeypatch, handler)
    models, error = probe.list_models("anthropic", "https://api.anthropic.com", "ak-test")

    assert error is None and models == ["claude-sonnet-4-5"]
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["key"] == "ak-test"


def test_list_models_returns_error_without_raising(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(401, json={"error": "unauthorized"}))
    models, error = probe.list_models("openai", "https://api.example.com/v1", "sk-bad")

    assert models == []
    assert error  # 非空错误提示，前端据此提示「列表不可用」


# ---- 联通测试 ----

def test_test_connection_openai_posts_chat_completions(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    _patch_client(monkeypatch, handler)
    ok, latency_ms, reply, error = probe.test_connection(
        "openai", "https://api.example.com/v1", "sk-test", "novel-pro")

    assert ok is True and error is None
    assert reply == "pong" and latency_ms >= 0
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["method"] == "POST"


def test_test_connection_anthropic_reads_text_blocks(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(
        200, json={"content": [{"type": "text", "text": "pong"}]}))
    ok, _latency, reply, error = probe.test_connection(
        "anthropic", "https://api.anthropic.com", "ak-test", "claude-x")

    assert ok is True and reply == "pong" and error is None


def test_test_connection_reports_failure(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(500, json={"error": "boom"}))
    ok, _latency, reply, error = probe.test_connection(
        "openai", "https://api.example.com/v1", "sk-test", "novel-pro")

    assert ok is False and reply is None and error


def test_test_connection_reports_provider_reason_and_redacts_key(monkeypatch):
    """上游 4xx 的具体原因（鉴权方式不对 / 模型不存在）只在响应体里，必须带出来。"""
    secret = "sk-relay-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": f"invalid key {secret}"}})

    _patch_client(monkeypatch, handler)
    ok, _latency, reply, error = probe.test_connection(
        "anthropic", "https://relay.example.com", secret, "claude-x")

    assert ok is False and reply is None
    assert "400" in (error or "") and "invalid key" in (error or "")
    assert secret not in (error or "")


def test_list_models_surfaces_plain_text_error_body(monkeypatch):
    """中转不一定回 JSON：非 JSON 响应体也要原样带出（截断脱敏后）。"""
    _patch_client(monkeypatch, lambda request: httpx.Response(404, text="404 page not found"))
    models, error = probe.list_models("anthropic", "https://relay.example.com", "ak-test")

    assert models == []
    assert "404" in (error or "") and "404 page not found" in (error or "")


def test_test_connection_rejects_non_object_json_body(monkeypatch):
    """2xx 但 body 不是 JSON 对象（误配到返回数组/字符串的端点）→ ok=False 且可诊断。

    此前 data.get 假定 dict，AttributeError 会逃出探针 → 路由 500，前端只看到「服务器错误」。
    """
    _patch_client(monkeypatch, lambda request: httpx.Response(200, json=["not", "an", "object"]))
    ok, _latency, reply, error = probe.test_connection(
        "openai", "https://misconfigured.example.com/v1", "sk-x", "novel-pro")

    assert ok is False and reply is None
    assert "不是 JSON 对象" in (error or "")



def test_probe_error_redacts_api_key(monkeypatch):
    secret = "sk-super-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect using {secret}")

    _patch_client(monkeypatch, handler)
    models, error = probe.list_models("openai", "https://api.example.com/v1", secret)

    assert models == []
    assert secret not in (error or "")
    assert "***" in (error or "")
