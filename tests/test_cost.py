"""成本估算：官方价目覆盖 DeepSeek / OpenAI / Anthropic / MiniMax。"""

from myink.providers.base import ModelResponse, effective_cost, estimate_cost, lookup_prices
from myink.providers.connections import CONNECTIONS_KEY, price_table, price_tables_from_packed
from myink.providers.prices import USD_CNY


def test_lookup_prices_official_families():
    assert lookup_prices("deepseek-flash") == lookup_prices("deepseek-v4-flash")
    assert lookup_prices("DeepSeek-V4-Flash-2026") == lookup_prices("deepseek-v4-flash")
    assert lookup_prices("gpt-4o-2024-11-20") == lookup_prices("gpt-4o")
    assert lookup_prices("claude-sonnet-4-5") == lookup_prices("claude-sonnet-4.5")
    assert lookup_prices("claude-sonnet-5") == lookup_prices("claude-sonnet")
    assert lookup_prices("MiniMax-M2.5") == lookup_prices("minimax-m2.5")
    assert lookup_prices("novel-pro") is None
    assert lookup_prices("stub") is None
    assert lookup_prices(None) is None


def test_deepseek_official_offpeak_cny():
    # 官方空闲：输入 ¥1 / 命中 ¥0.02 / 输出 ¥4
    assert estimate_cost("deepseek-flash", 1_000_000, 1_000_000, cache_hit=False) == 5.0
    assert estimate_cost("deepseek-flash", 1_000_000, 0, cache_hit=True) == 0.02
    assert estimate_cost("deepseek-v4-pro", 1_000_000, 0, cache_hit=False) == 4.5


def test_openai_and_anthropic_convert_usd():
    assert estimate_cost("gpt-4o", 1_000_000, 0, cache_hit=False) == 2.5 * USD_CNY
    assert estimate_cost("claude-sonnet-5", 1_000_000, 0, cache_hit=False) == 2.0 * USD_CNY
    assert estimate_cost("unknown-model", 9_000, 9_000) == 0.0


def test_model_response_uses_official_price():
    resp = ModelResponse(
        content="ok", model_id="deepseek-flash",
        input_tokens=1_000_000, output_tokens=500_000, cache_hit=False,
    )
    assert resp.cost_est == 3.0


def test_connection_prices_override_catalog():
    resp = ModelResponse(
        content="ok", model_id="novel-pro",
        input_tokens=1_000_000, output_tokens=500_000, cache_hit=False,
        prices={"input": 3.0, "input_cache_hit": 3.0, "output": 6.0},
    )
    assert resp.cost_est == 6.0
    assert price_table({"input_price": 1, "output_price": 2}) == {
        "input": 1.0, "input_cache_hit": 1.0, "output": 2.0,
    }
    assert price_tables_from_packed({
        CONNECTIONS_KEY: {"c1": {"model": "novel-pro", "input_price": 3, "output_price": 9}},
    }) == {"novel-pro": {"input": 3.0, "input_cache_hit": 3.0, "output": 9.0}}


def test_effective_cost_keeps_stored_and_backfills_zero():
    assert effective_cost(cost_est=0.12, model_id="deepseek-flash", input_tokens=10) == 0.12
    assert effective_cost(
        cost_est=0.0, model_id="deepseek-flash",
        input_tokens=1_000_000, output_tokens=0, cache_hit=False,
    ) == 1.0
    assert effective_cost(cost_est=0.0, model_id="stub", input_tokens=100, output_tokens=200) == 0.0
