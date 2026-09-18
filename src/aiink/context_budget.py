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


def estimate_tokens(value: object) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return math.ceil(len(text) * 1.4)


def fit_prompt(context: dict, render: Callable[[dict], list[dict]], budget: int) -> list[dict]:
    # 不裁掉 checkpoint / 批次共享记忆，只复制并选择本次请求的条目。
    selected = {key: list(value) if isinstance(value, list) else value for key, value in context.items()}
    # 可选记忆单独限额；正文和必需约束使用完整请求预算。
    from aiink.config import settings
    required = dict(selected)
    for key in ('mid_term_events', 'reflexions', 'plot_threads', 'open_foreshadows', 'recent_openings', 'entity_snapshots', 'setting_snapshots'):
        required[key] = []
    required['long_term_facts'] = [r for r in selected.get('long_term_facts', []) if r.get('is_hard', True)]
    budget = min(budget, estimate_tokens(render(required)) + settings.recall_token_budget)
    messages = render(selected)
    before = estimate_tokens(messages)
    dropped: dict[str, int] = {}
    # 优先保留硬约束、作者指令、前章接续和本章人物；近期项保持原顺序。
    # 键序 = 裁剪序（靠前者先丢）。设定实体排在人物快照之前——两者都是「设定侧」背景，
    # 但人物台账是连贯性的核心，设定（物品/功法/地点）先让位。
    for key in ('long_term_facts', 'mid_term_events', 'reflexions',
                'plot_threads', 'open_foreshadows', 'recent_openings',
                'setting_snapshots', 'entity_snapshots'):
        rows = selected.get(key, [])
        for index in range(len(rows) - 1, -1, -1):
            if estimate_tokens(messages) <= budget:
                break
            if key == 'long_term_facts' and rows[index].get('is_hard', True):
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
