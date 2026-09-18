"""官方模型单价（¥/百万 token），按 model_id 估算章节费用。

来源（2026-09）：
- DeepSeek 中文价目：空闲时段（高峰是其 2 倍，估算用空闲）；
- OpenAI / Anthropic / MiniMax：官方美元标价 × USD_CNY。
中转站自定 id 不在表内 → 估 0，不瞎编。
"""

from __future__ import annotations

# 界面统一 ¥。美元官方价按此折算（2026-09 估算，不跟盘）。
USD_CNY = 7.2


def _cny(inp: float, hit: float, out: float) -> dict[str, float]:
    return {"input": inp, "input_cache_hit": hit, "output": out}


def _usd(inp: float, hit: float, out: float) -> dict[str, float]:
    return _cny(inp * USD_CNY, hit * USD_CNY, out * USD_CNY)


# DeepSeek 官方人民币 · 空闲时段（api-docs.deepseek.com/zh-cn/quick_start/pricing）
_DS_FLASH = _cny(1.0, 0.02, 4.0)
_DS_PRO = _cny(4.5, 0.15, 13.5)

# 精确 id（含常见别名）。前缀族见 _FAMILIES，按更长者优先。
EXACT: dict[str, dict[str, float]] = {
    "deepseek-flash": _DS_FLASH,
    "deepseek-v4-flash": _DS_FLASH,
    "deepseek-v4-flash-vision-exp": _DS_FLASH,
    "deepseek-v4-pro": _DS_PRO,
    "deepseek-pro": _DS_PRO,
}

# OpenAI 官方标准短上下文（developers.openai.com/api/docs/pricing 与各模型页）
_FAMILIES: tuple[tuple[str, dict[str, float]], ...] = (
    ("gpt-6-astra", _usd(10.00, 1.00, 50.00)),
    ("gpt-5.6-cyber", _usd(12.50, 1.25, 75.00)),
    ("gpt-5.6-sol", _usd(4.00, 0.40, 20.00)),
    ("gpt-5.6-terra", _usd(2.00, 0.20, 12.00)),
    ("gpt-5.6-luna", _usd(0.20, 0.02, 1.20)),
    ("gpt-5.3-codex", _usd(1.75, 0.175, 14.00)),
    ("gpt-5-mini", _usd(0.25, 0.025, 2.00)),  # 输入来自 GPT-5 模型页对照；输出按同页 1:8
    ("gpt-5-nano", _usd(0.05, 0.005, 0.40)),
    ("gpt-5", _usd(1.25, 0.125, 10.00)),
    ("gpt-4.1", _usd(2.00, 0.50, 8.00)),
    ("gpt-4o-mini", _usd(0.15, 0.075, 0.60)),
    ("gpt-4o", _usd(2.50, 1.25, 10.00)),
    # Anthropic 官方（platform.claude.com/docs/en/about-claude/pricing）
    ("claude-fable-5-1", _usd(10.00, 0.25, 50.00)),
    ("claude-fable-5.1", _usd(10.00, 0.25, 50.00)),
    ("claude-mythos-5-1", _usd(10.00, 0.25, 50.00)),
    ("claude-mythos-5.1", _usd(10.00, 0.25, 50.00)),
    ("claude-fable", _usd(10.00, 1.00, 50.00)),
    ("claude-mythos", _usd(10.00, 1.00, 50.00)),
    ("claude-opus-4-1", _usd(15.00, 1.50, 75.00)),
    ("claude-opus-4.1", _usd(15.00, 1.50, 75.00)),
    ("claude-opus-4", _usd(15.00, 1.50, 75.00)),
    ("claude-opus", _usd(5.00, 0.50, 25.00)),  # Opus 5 / 4.5–4.8
    ("claude-sonnet-4-6", _usd(3.00, 0.30, 15.00)),
    ("claude-sonnet-4.6", _usd(3.00, 0.30, 15.00)),
    ("claude-sonnet-4-5", _usd(3.00, 0.30, 15.00)),
    ("claude-sonnet-4.5", _usd(3.00, 0.30, 15.00)),
    ("claude-sonnet-4", _usd(3.00, 0.30, 15.00)),
    ("claude-sonnet", _usd(2.00, 0.20, 10.00)),  # Sonnet 5 现行标准价
    ("claude-haiku-4-5", _usd(1.00, 0.10, 5.00)),
    ("claude-haiku-4.5", _usd(1.00, 0.10, 5.00)),
    ("claude-haiku-3-5", _usd(0.80, 0.08, 4.00)),
    ("claude-haiku-3.5", _usd(0.80, 0.08, 4.00)),
    ("claude-haiku", _usd(1.00, 0.10, 5.00)),
    # MiniMax 官方（platform.minimax.io 标准档 ≤512k）
    ("minimax-m2.7-highspeed", _usd(0.60, 0.06, 2.40)),
    ("minimax-m2.5-highspeed", _usd(0.60, 0.03, 2.40)),
    ("minimax-m2.1-highspeed", _usd(0.60, 0.03, 2.40)),
    ("minimax-m3", _usd(0.30, 0.06, 1.20)),
    ("minimax-m2.7", _usd(0.30, 0.06, 1.20)),
    ("minimax-m2.5", _usd(0.30, 0.03, 1.20)),
    ("minimax-m2.1", _usd(0.30, 0.03, 1.20)),
    ("minimax-m2", _usd(0.30, 0.03, 1.20)),
    # DeepSeek 族（精确表未命中时）
    ("deepseek-v4-pro", _DS_PRO),
    ("deepseek-pro", _DS_PRO),
    ("deepseek-v4-flash", _DS_FLASH),
    ("deepseek-flash", _DS_FLASH),
)

_FAMILIES_SORTED = tuple(sorted(_FAMILIES, key=lambda item: len(item[0]), reverse=True))

# 兼容旧 import：仅 Flash / Pro 空闲价。
DEEPSEEK_PRICES: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": _DS_FLASH,
    "deepseek-flash": _DS_FLASH,
    "deepseek-v4-pro": _DS_PRO,
    "deepseek-pro": _DS_PRO,
}


def _normalize(model_id: str) -> str:
    key = model_id.strip().lower().split("/")[-1]
    return key.replace("_", "-")


def lookup_prices(model_id: str | None) -> dict[str, float] | None:
    """按官方价目取单价；未知模型返回 None。"""
    if not model_id or not model_id.strip():
        return None
    key = _normalize(model_id)
    hit = EXACT.get(key)
    if hit is not None:
        return hit
    for prefix, table in _FAMILIES_SORTED:
        if key == prefix or key.startswith(prefix + "-") or key.startswith(prefix + "."):
            return table
    if key.startswith("deepseek") and "pro" in key:
        return _DS_PRO
    if key.startswith("deepseek") and "flash" in key:
        return _DS_FLASH
    return None
