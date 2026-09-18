"""ModelProvider 层单测（§19.3 思考模式兜底 + §6.10 降级链）。

无需真实 DeepSeek 调用：monkeypatch OpenAI client 的 create，验证：
- content 非空 → 原样透传（兜底不误伤正常路径）；
- content 空/全空白但 reasoning_content 有内容 → 兜底（v4 `thinking: disabled` 偶发不生效，
  §19.3 思考 token 仍产生，正文进 reasoning_content，否则下游 _parse_json("") 崩整章；
  实测 json_mode 最终轮偶发返回「全空白占位 content」，`not content` 抓不住）；
- 两者皆空 → error（触发 FallbackChain 降级重试，§6.12 ① 模型降级链）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from myink.providers.base import ModelResponse
from myink.providers.deepseek import DeepSeekProvider, MAX_RETRIES


def _fake_create(content: str = "", reasoning_content: str = ""):
    """构造模拟 OpenAI 响应的 create 函数（返回 usage/choices/message）。"""
    def create(**kwargs):
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                prompt_cache_hit_tokens=0)
        message = SimpleNamespace(content=content or None,
                                  reasoning_content=reasoning_content or None,
                                  tool_calls=None)
        return SimpleNamespace(usage=usage,
                               choices=[SimpleNamespace(message=message)])
    return create


def _provider_with(create):
    p = DeepSeekProvider(api_key="test-key")
    p._client.chat.completions.create = create
    return p


def test_leading_think_block_stripped_from_chapter():
    p = _provider_with(_fake_create(content="<think>先推敲</think>\n他推开门。"))
    resp: ModelResponse = p.generate(
        [{"role": "user", "content": "x"}], model_id="novel-pro",
        disable_thinking=True, json_mode=False,
    )
    assert resp.error is None
    assert resp.content == "他推开门。"


def test_content_nonempty_passthrough():
    """content 非空：正常路径原样返回，不触发兜底。"""
    p = _provider_with(_fake_create(content='{"verdict": "pass"}'))
    resp: ModelResponse = p.generate([{"role": "user", "content": "x"}], model_id="deepseek-v4-flash")
    assert resp.error is None
    assert resp.content == '{"verdict": "pass"}'


def test_write_path_does_not_use_reasoning_as_chapter():
    p = _provider_with(_fake_create(content="", reasoning_content="我先想一下剧情再写"))
    resp: ModelResponse = p.generate(
        [{"role": "user", "content": "x"}], model_id="deepseek-flash",
        disable_thinking=True, json_mode=False,
    )
    assert resp.error is not None
    assert "思考过程" in resp.error
    # 思考开着也不把 reasoning 当章节（inkos：只拆字段，不并入正文）
    resp_on = p.generate(
        [{"role": "user", "content": "x"}], model_id="deepseek-flash",
        disable_thinking=False, json_mode=False,
    )
    assert resp_on.error is not None
    assert "思考过程" in resp_on.error


def test_reasoning_content_fallback():
    """JSON 才允许 content 空时借用 reasoning（audit 偶发把 JSON 放进思考字段）。"""
    p = _provider_with(_fake_create(content="", reasoning_content='思考过程... {"verdict": "pass"}'))
    resp: ModelResponse = p.generate(
        [{"role": "user", "content": "x"}], model_id="deepseek-v4-flash", json_mode=True,
    )
    assert resp.error is None
    assert resp.content.startswith("思考过程")
    assert "verdict" in resp.content


def test_blank_content_falls_back():
    """content 全空白（json_mode 最终轮占位输出，122 空格）+ reasoning 有值：兜底。"""
    p = _provider_with(_fake_create(content=" " * 122, reasoning_content='思考... {"verdict": "pass"}'))
    resp: ModelResponse = p.generate(
        [{"role": "user", "content": "x"}], model_id="deepseek-v4-flash", json_mode=True,
    )
    assert resp.error is None
    assert resp.content.startswith("思考")
    assert "verdict" in resp.content


def test_both_empty_returns_error():
    """content 与 reasoning_content 皆空：返回 error 触发 FallbackChain 降级重试（§6.12 ①）。"""
    p = _provider_with(_fake_create(content="", reasoning_content=""))
    resp: ModelResponse = p.generate([{"role": "user", "content": "x"}], model_id="deepseek-v4-flash")
    assert resp.error is not None
    assert "空白/空内容" in resp.error


def test_empty_content_retries_then_error():
    """皆空：在既有循环内快速重试 MAX_RETRIES 次（不 sleep），用尽才返回 error。

    回归（2026-08-22）：此前皆空立即返回 error，flash→pro 两条模型同病就白给；现在
    快速重发可自愈瞬时输出异常，用尽仍返回 error 触发 FallbackChain 降级链。
    """
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                prompt_cache_hit_tokens=0)
        message = SimpleNamespace(content=None, reasoning_content=None, tool_calls=None)
        return SimpleNamespace(usage=usage,
                               choices=[SimpleNamespace(message=message)])

    p = _provider_with(create)
    resp: ModelResponse = p.generate([{"role": "user", "content": "x"}], model_id="deepseek-v4-flash")
    assert calls["n"] == MAX_RETRIES + 1, f"皆空应重试 {MAX_RETRIES} 次后返回 error，实际 {calls['n']} 次"
    assert resp.error is not None
    assert "空白/空内容" in resp.error
