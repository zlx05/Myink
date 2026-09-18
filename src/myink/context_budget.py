"""按完整提示词估算预算，整条移除可选记忆，不截断硬约束或正文。

沿用中文约 1.4 token/字的启发式估算，不冒充模型 tokenizer 的精确计数。
真实用量仍由 Provider usage 写入 agent_runs。
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ContextBudgetExceeded(ValueError):
    pass


# ---- 记忆分层单一真源（spec/schema.md §12）----
# 「哪块记忆属于哪一层、能不能丢、先丢谁、进哪个节点」此前散在三处各写一份：本文件的
# 两个硬编码列表、prompts.py 各 _*_messages 的函数体、spec 里的散文。三份已经漂移过
# （§12 连 open_foreshadows / plot_threads 两个字段都没列）。现在只留这一份：
# fit_prompt 的裁剪从它派生，tests/test_memory_layers.py 断言 prompts.py 的实际注入与
# NODE_INJECTIONS 一致——漂移即红。
#
# layer       记忆的时间尺度：long 长期 / mid 中期 / short 短期 / world 设定侧快照
# droppable   超预算时可整块移除；short_context 是前章接续与作者指令，永不丢
# drop_order  裁剪序，小的先丢（long_term_facts 排 0 = 非 hard 事实最先让位）
# hard_exempt 该块的行标了 is_hard 就永不丢（逐行判断，见 fit_prompt）
MEMORY_LAYERS: dict[str, dict] = {
    "long_term_facts":   {"layer": "long",  "droppable": True, "drop_order": 0, "hard_exempt": True},
    "mid_term_events":   {"layer": "mid",   "droppable": True, "drop_order": 1},
    "reflexions":        {"layer": "long",  "droppable": True, "drop_order": 2},
    "plot_threads":      {"layer": "mid",   "droppable": True, "drop_order": 3},
    "open_foreshadows":  {"layer": "mid",   "droppable": True, "drop_order": 4},
    "recent_openings":   {"layer": "short", "droppable": True, "drop_order": 5},
    "setting_snapshots": {"layer": "world", "droppable": True, "drop_order": 6},
    "entity_snapshots":  {"layer": "world", "droppable": True, "drop_order": 7},
    "short_context":     {"layer": "short", "droppable": False},
}

# 各节点注入哪些记忆块。键 = 图节点名（workflow/nodes.py）；值的差异都是有意的，不是遗漏：
# - write 不注入 mid_term_events / open_foreshadows / plot_threads：它在 plan_chapter 之后
#   跑，这三块已由章节计划承接（计划里写明本章推进哪条线、收哪个伏笔），重注是重复计费；
# - revise 复用 write 的拼装，注入面随之相同；
# - plan_cast 不注入 entity_snapshots 与 reflexions：这一拍只决定「谁出场、在哪」，依据是
#   角色名单 + 设定实体；台账快照恰是本拍之后才去取的东西（鸡生蛋），而 reflexions 是
#   节奏/伏笔类技法建议，可执行点在 plan_chapter 与 write；
# - extract 只收台账快照、开放伏笔与作者短指令（short_context 里 kind 为 author_review /
#   user_instruction 的行），不收事件与剧情线；
# - summarize 只吃正文，不注入召回上下文。
NODE_INJECTIONS: dict[str, tuple[str, ...]] = {
    "plan_cast": ("long_term_facts", "mid_term_events", "setting_snapshots",
                  "open_foreshadows", "plot_threads", "recent_openings", "short_context"),
    "plan_chapter": ("long_term_facts", "mid_term_events", "entity_snapshots",
                     "setting_snapshots", "open_foreshadows", "plot_threads", "reflexions",
                     "recent_openings", "short_context"),
    "write": ("long_term_facts", "entity_snapshots", "setting_snapshots", "reflexions",
              "recent_openings", "short_context"),
    "revise": ("long_term_facts", "entity_snapshots", "setting_snapshots", "reflexions",
               "recent_openings", "short_context"),
    "extract": ("entity_snapshots", "open_foreshadows", "short_context"),
    "audit": ("long_term_facts", "mid_term_events", "entity_snapshots", "setting_snapshots",
              "open_foreshadows", "plot_threads", "recent_openings", "short_context"),
    "summarize": (),
}

_DROPPABLE: tuple[str, ...] = tuple(k for k, v in MEMORY_LAYERS.items() if v["droppable"])
# 键序 = 裁剪序：靠前者先丢。
_DROP_ORDER: tuple[str, ...] = tuple(
    sorted(_DROPPABLE, key=lambda k: MEMORY_LAYERS[k]["drop_order"]))


def estimate_tokens(value: object) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return math.ceil(len(text) * 1.4)


def fit_prompt(context: dict, render: Callable[[dict], list[dict]], budget: int) -> list[dict]:
    # 不裁掉 checkpoint / 批次共享记忆，只复制并选择本次请求的条目。
    selected = {key: list(value) if isinstance(value, list) else value for key, value in context.items()}
    # 可选记忆单独限额；正文和必需约束使用完整请求预算。
    from myink.config import settings
    required = dict(selected)
    for key in _DROPPABLE:
        if MEMORY_LAYERS[key].get("hard_exempt"):
            required[key] = [r for r in selected.get(key, []) if r.get("is_hard", True)]
        else:
            required[key] = []
    budget = min(budget, estimate_tokens(render(required)) + settings.recall_token_budget)
    messages = render(selected)
    before = estimate_tokens(messages)
    dropped: dict[str, int] = {}
    # 优先保留硬约束、作者指令、前章接续和本章人物；近期项保持原顺序。
    # 裁剪序由 MEMORY_LAYERS.drop_order 声明（见该表注释）。
    for key in _DROP_ORDER:
        rows = selected.get(key, [])
        for index in range(len(rows) - 1, -1, -1):
            if estimate_tokens(messages) <= budget:
                break
            if MEMORY_LAYERS[key].get("hard_exempt") and rows[index].get("is_hard", True):
                continue
            rows.pop(index)
            dropped[key] = dropped.get(key, 0) + 1
            messages = render(selected)
    after = estimate_tokens(messages)
    if after > budget:
        raise ContextBudgetExceeded(
            f'CONTEXT_BUDGET_EXCEEDED: 必需内容估算 {after} tokens，预算 {budget}；'
            '请精简硬约束/本章输入或调高预算，系统未截断正文和硬约束。')
    if dropped:
        logger.info('prompt_budget before_est=%d after_est=%d budget=%d dropped=%s',
                    before, after, budget, dropped)
    return messages
