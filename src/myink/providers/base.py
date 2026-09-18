"""ModelProvider 抽象 + 模型注册表 + 降级链（plan.md §6.10 / §19.3）。

- 模型注册表：model_id → 上下文窗口 / 输出上限 / 成本档 / 能力标记；
- 降级链：每个 role 配 fallback（主 → 备 → 全局默认），失败自动降级不中断写作；
- 成本估算：cost_est = 输入token×(命中?命中价:未命中价) + 输出token×输出价。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from myink.providers.prices import lookup_prices


def estimate_cost(
    model_id: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_hit: bool = False,
    prices: dict[str, float] | None = None,
) -> float:
    """成本估算（¥）：输入×(命中?命中价:未命中价) + 输出×输出价。"""
    table = prices if prices is not None else lookup_prices(model_id)
    if table is None:
        return 0.0
    in_price = table["input_cache_hit"] if cache_hit else table["input"]
    return (input_tokens * in_price + output_tokens * table["output"]) / 1_000_000


def effective_cost(
    *,
    cost_est: float | None,
    model_id: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_hit: bool = False,
    prices: dict[str, float] | None = None,
) -> float:
    """已写入的非零 cost 原样用；历史 0 行按连接单价或内置表回算。"""
    stored = float(cost_est or 0.0)
    if stored:
        return stored
    return estimate_cost(model_id, input_tokens, output_tokens, cache_hit, prices=prices)


@dataclass(frozen=True)
class ModelSpec:
    """模型注册表一项（§6.10）。"""

    model_id: str
    context_window: int
    max_output: int
    capabilities: frozenset[str] = frozenset({"llm", "json"})


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "deepseek-v4-flash": ModelSpec("deepseek-v4-flash", 1_000_000, 384_000, frozenset({"llm", "json"})),
    "deepseek-v4-pro": ModelSpec("deepseek-v4-pro", 1_000_000, 384_000, frozenset({"llm", "json"})),
}


@dataclass
class ModelResponse:
    """一次 LLM 调用结果（§6.8 agent_runs 全字段的数据来源）。"""

    content: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0  # 思考 token 计入输出（§19.3）
    cache_hit: bool = False
    duration_ms: int = 0
    retry_count: int = 0
    degraded: bool = False
    error: str | None = None
    # 工具调用（§10 只读查证工具）：[{id, name, arguments(dict)}]，None = 本轮无工具调用
    tool_calls: list[dict] | None = None
    # 连接上的自定义单价（¥/百万 token）；None 则回落到 lookup_prices(model_id)
    prices: dict[str, float] | None = None

    @property
    def cost_est(self) -> float:
        """成本估算（¥）。"""
        return estimate_cost(
            self.model_id, self.input_tokens, self.output_tokens, self.cache_hit,
            prices=self.prices,
        )


class ModelProvider(ABC):
    """模型提供方抽象：可切换 / 降级（§3 已收敛决策）。"""

    @abstractmethod
    def generate(self, messages: list[dict], *, model_id: str, max_tokens: int | None = None,
                 temperature: float | None = None, json_mode: bool = False,
                 tools: list[dict] | None = None,
                 disable_thinking: bool = False) -> ModelResponse:
        ...

    def generate_stream(self, messages: list[dict], *, model_id: str,
                        on_delta: Callable[[str], None],
                        on_reset: Callable[[], None] | None = None,
                        max_tokens: int | None = None,
                        temperature: float | None = None, json_mode: bool = False,
                        tools: list[dict] | None = None,
                        disable_thinking: bool = False) -> ModelResponse:
        """流式生成的兼容入口。

        测试桩和暂不支持流式的 Provider 仍可只实现 ``generate``；默认实现会在完整
        响应返回后发送一个片段。真实 Provider 覆盖此方法即可提供增量输出。
        """
        resp = self.generate(
            messages, model_id=model_id, max_tokens=max_tokens,
            temperature=temperature, json_mode=json_mode, tools=tools,
            disable_thinking=disable_thinking,
        )
        if resp.content:
            on_delta(resp.content)
        return resp

    @abstractmethod
    def name(self) -> str:
        ...


class FallbackChain:
    """降级链：主 → 备 → 全局默认（§6.10/§6.12 调用层）。"""

    def __init__(self, provider: ModelProvider, chain: list[str],
                 providers: list[ModelProvider] | None = None,
                 thinking_enabled: bool = False,
                 prices: dict[str, float] | None = None):
        if providers is not None and len(providers) != len(chain):
            raise ValueError("providers 与 chain 长度必须一致")
        self.provider = provider
        self.chain = chain
        self.providers = providers or [provider] * len(chain)
        self.thinking_enabled = thinking_enabled
        self.prices = prices

    def _stamp(self, resp: ModelResponse, *, degraded: bool) -> ModelResponse:
        resp.degraded = degraded
        if self.prices is not None:
            resp.prices = self.prices
        return resp

    def generate(self, messages: list[dict], *, json_mode: bool = False, max_tokens: int | None = None,
                 temperature: float | None = None, tools: list[dict] | None = None,
                 disable_thinking: bool = False) -> ModelResponse:
        if not self.thinking_enabled:
            disable_thinking = True
        last_error: str | None = None
        for i, (provider, model_id) in enumerate(zip(self.providers, self.chain, strict=True)):
            try:
                resp = provider.generate(
                    messages, model_id=model_id, max_tokens=max_tokens,
                    temperature=temperature, json_mode=json_mode, tools=tools,
                    disable_thinking=disable_thinking,
                )
                if resp.error is None:
                    return self._stamp(resp, degraded=i > 0)
                last_error = resp.error
            except Exception as exc:  # 网络 / 超时 / 5xx → 试下一个
                last_error = str(exc)
        return ModelResponse(content="", model_id=self.chain[-1], error=last_error)

    def generate_stream(self, messages: list[dict], *, on_delta: Callable[[str], None],
                        on_reset: Callable[[], None] | None = None,
                        json_mode: bool = False, max_tokens: int | None = None,
                        temperature: float | None = None, tools: list[dict] | None = None,
                        disable_thinking: bool = False) -> ModelResponse:
        if not self.thinking_enabled:
            disable_thinking = True
        last_error: str | None = None
        for i, (provider, model_id) in enumerate(zip(self.providers, self.chain, strict=True)):
            if i and on_reset:
                on_reset()
            try:
                resp = provider.generate_stream(
                    messages, model_id=model_id, on_delta=on_delta, on_reset=on_reset,
                    max_tokens=max_tokens, temperature=temperature,
                    json_mode=json_mode, tools=tools, disable_thinking=disable_thinking,
                )
                if resp.error is None:
                    return self._stamp(resp, degraded=i > 0)
                last_error = resp.error
            except Exception as exc:
                last_error = str(exc)
        return ModelResponse(content="", model_id=self.chain[-1], error=last_error)


# 可配置角色：环境配置里用户可为这些 role 指定自己的模型。
CONFIGURABLE_ROLES: frozenset[str] = frozenset({
    "planner", "writer", "validator_l2", "extract", "audit", "summarize",
})


# 已废弃：不再内置 DeepSeek 默认链。测试仍可能 setitem，生产路径不读此表。
DEFAULT_ROUTES: dict[str, list[str]] = {}


class MissingModelProvider(ModelProvider):
    """用户未配置连接时的显式失败，避免悄悄打 .env 里的内置 Key。"""

    def generate(self, messages: list[dict], *, model_id: str, max_tokens: int | None = None,
                 temperature: float | None = None, json_mode: bool = False,
                 tools: list[dict] | None = None,
                 disable_thinking: bool = False) -> ModelResponse:
        return ModelResponse(
            content="", model_id=model_id,
            error="请先在环境配置里添加模型连接，并为该角色指定模型",
        )

    def name(self) -> str:
        return "unconfigured"
