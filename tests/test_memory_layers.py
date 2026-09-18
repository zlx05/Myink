"""记忆分层的单一真源（context_budget.MEMORY_LAYERS / NODE_INJECTIONS）。

「哪块记忆进哪个节点、超预算先丢谁」过去散在三处各写一份：context_budget 的两个硬编码
列表、prompts.py 各 `_*_messages` 的函数体、spec 里的散文；三份已经漂移过（§12 连
open_foreshadows / plot_threads 都没列）。本套件把那三份钉成一份：

- **注入面**：给每个记忆块塞一个唯一哨兵串，调各节点的公开 messages 构造函数，断言
  哨兵出现 ⟺ 该块在 `NODE_INJECTIONS[node]` 里。prompts.py 悄悄多注或漏注一块即红。
- **完整性**：声明表须覆盖 RetrievedContext 的每一个记忆块字段，且不出现表外的块名
  （新增记忆块不登记就失败）。
- **裁剪序**：预算压到只够「必需内容」时，按 drop_order 从小到大抽干；is_hard 事实与
  short_context 一行不掉。

本套件只锚定「谁注入谁」，不锚定渲染文案——文案变了不该红。
"""

from __future__ import annotations

import json

import pytest

from myink.config import settings
from myink.context_budget import (
    _DROP_ORDER,
    MEMORY_LAYERS,
    NODE_INJECTIONS,
    estimate_tokens,
    fit_prompt,
)
from myink.schemas.contract import RetrievedContext
from myink.workflow import prompts

# 每个记忆块注哨兵的字段：必须是该块渲染器真正读的字段，否则哨兵不出现，断言会假红。
_SENTINEL_FIELD: dict[str, tuple[str, dict]] = {
    "long_term_facts":   ("content", {"is_hard": True}),
    "mid_term_events":   ("summary", {}),
    "entity_snapshots":  ("name", {}),
    "setting_snapshots": ("name", {}),
    "open_foreshadows":  ("description", {}),
    "plot_threads":      ("name", {}),
    "recent_openings":   ("text", {"chapter": 1}),
    "reflexions":        ("content", {"lesson_type": "both"}),   # 两个通道都命中
    "short_context":     ("text", {"kind": "author_review"}),    # extract 只收作者类短指令
}

# 非记忆块字段：召回上下文的元信息，不属于分层讨论范围。
_NON_BLOCK_FIELDS = {"token_usage", "recall_stats"}


def _sentinel(block: str) -> str:
    return f"__SENTINEL_{block}__"


def _context() -> dict:
    """每块一行哨兵——块若被注入，哨兵必然出现在提示词里。"""
    ctx: dict = {}
    for block, (field, extra) in _SENTINEL_FIELD.items():
        ctx[block] = [{field: _sentinel(block), **extra}]
    return ctx


def _node_messages(node: str, context: dict) -> list[dict]:
    """节点 → 生产实际调用的那个公开构造函数（含 fit_prompt），不测内部函数。"""
    if node == "plan_cast":
        return prompts.cast_messages(context, ["甲", "乙"])
    if node == "plan_chapter":
        return prompts.plan_messages(context, None, None)
    if node == "write":
        return prompts.write_messages(context, {})
    if node == "revise":
        return prompts.revise_messages("正文", [], 1, context=context, plan={})
    if node == "extract":
        return prompts.extract_messages("正文", 1, context)
    if node == "audit":
        return prompts.audit_messages("正文", {}, context, 1)
    if node == "summarize":
        return prompts.summary_messages("正文", 1)
    raise AssertionError(f"未登记的节点：{node}")


def _text(messages: list[dict]) -> str:
    return "\n".join(m["content"] for m in messages)


# ---- 注入面 ----


def test_declared_nodes_are_all_covered():
    """声明表里的节点必须都能取到提示词——漏一个就说明表在说空话。"""
    for node in NODE_INJECTIONS:
        assert _node_messages(node, _context())


@pytest.mark.parametrize("node", sorted(NODE_INJECTIONS))
def test_node_injects_exactly_the_declared_blocks(node):
    """哨兵出现 ⟺ 该块被声明注入本节点。核心断言：prompts.py 与声明表不许各说各话。"""
    context = _context()
    text = _text(_node_messages(node, context))
    declared = set(NODE_INJECTIONS[node])
    for block in _SENTINEL_FIELD:
        present = _sentinel(block) in text
        assert present == (block in declared), (
            f"{node}: 块 {block} 实际{'注入' if present else '未注入'}，"
            f"但声明表说{'注入' if block in declared else '不注入'}")


def test_write_lets_the_plan_carry_events_and_hooks():
    """write 不注 mid_term_events / open_foreshadows / plot_threads 是有意的：它在
    plan_chapter 之后跑，这三块由章节计划承接。此处显式命名，防有人当遗漏补上。"""
    assert {"mid_term_events", "open_foreshadows", "plot_threads"}.isdisjoint(
        NODE_INJECTIONS["write"])
    assert {"mid_term_events", "open_foreshadows", "plot_threads"}.issubset(
        NODE_INJECTIONS["plan_chapter"]), "承接的前提是 plan_chapter 真的看得到"


def test_cast_stays_lean():
    """plan_cast 不注 entity_snapshots（台账是本拍之后才取的，鸡生蛋）也不注 reflexions
    （技法建议的执行点在 plan/write，注进取名步只有成本没有决策面）。"""
    assert {"entity_snapshots", "reflexions"}.isdisjoint(NODE_INJECTIONS["plan_cast"])


# ---- 完整性 ----


def test_declaration_covers_every_context_block():
    """RetrievedContext 的每个记忆块字段都要有分层声明；新增块不登记即红。"""
    blocks = set(RetrievedContext.model_fields) - _NON_BLOCK_FIELDS
    assert set(MEMORY_LAYERS) == blocks


def test_injections_only_name_declared_blocks():
    """NODE_INJECTIONS 只能引用已声明的块，不许凭空出现块名。"""
    named = {b for blocks in NODE_INJECTIONS.values() for b in blocks}
    assert named <= set(MEMORY_LAYERS)


def test_short_context_is_the_only_undeclarable_drop():
    """short_context 载着前章接续与作者指令，是唯一永不丢的块。"""
    undroppable = {k for k, v in MEMORY_LAYERS.items() if not v["droppable"]}
    assert undroppable == {"short_context"}


def test_drop_order_is_a_total_order():
    """drop_order 必须唯一，裁剪序才是确定的（同序会让排序退化成字典序）。"""
    orders = [MEMORY_LAYERS[k]["drop_order"] for k in _DROP_ORDER]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)
    assert set(_DROP_ORDER) == {k for k, v in MEMORY_LAYERS.items() if v["droppable"]}


# ---- 裁剪序 ----


def _render(context: dict) -> list[dict]:
    return [{"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}]


def _kept(context: dict, budget: int) -> dict:
    """fit_prompt 只复制不就地改，所以裁剪结果要看**返回的**消息，不能看入参。"""
    return json.loads(fit_prompt(context, _render, budget)[0]["content"])


def _big_rows(prefix: str, n: int = 3, *, hard: bool = False) -> list[dict]:
    row = {"content": f"{prefix} " + "字" * 400, "summary": f"{prefix} " + "字" * 400,
           "name": f"{prefix} " + "字" * 400, "description": f"{prefix} " + "字" * 400,
           "text": f"{prefix} " + "字" * 400, "chapter": 1, "lesson_type": "both",
           "kind": "author_review", "is_hard": hard}
    return [dict(row) for _ in range(n)]


def _floor_budget(context: dict) -> int:
    """只够「必需内容」的预算：非 hard 行与可丢块全部按零计，与现实同款估算。"""
    required = dict(context)
    for key in _DROP_ORDER:
        required[key] = ([r for r in context.get(key, []) if r.get("is_hard", True)]
                         if MEMORY_LAYERS[key].get("hard_exempt") else [])
    return estimate_tokens(_render(required)) + settings.recall_token_budget


def _required_only_budget(context: dict) -> int:
    """只够硬约束 + short_context 的预算（不含 recall 额度）——逼裁剪把可丢块全抽干。"""
    required = dict(context)
    for key in _DROP_ORDER:
        required[key] = ([r for r in context.get(key, []) if r.get("is_hard", True)]
                         if MEMORY_LAYERS[key].get("hard_exempt") else [])
    return estimate_tokens(_render(required))


def _holds_droppable(kept: dict, key: str) -> bool:
    """该块是否还留着「可被裁掉」的行。hard_exempt 块只留 hard 行不算幸存。"""
    rows = kept[key]
    if not MEMORY_LAYERS[key].get("hard_exempt"):
        return bool(rows)
    return any(not r.get("is_hard", True) for r in rows)


def test_drop_order_drains_lower_orders_first():
    """预算压到刚好够必需内容 → 按 drop_order 从小到大抽干：幸存的块必是裁剪序的**后缀**
    （某块还留着可丢行 ⇒ 排在它前面的可丢行已经空了）。"""
    context = {key: _big_rows(key) for key in _DROP_ORDER}
    context["long_term_facts"] = _big_rows("long_term_facts") + _big_rows("hard_fact", 1, hard=True)
    context["short_context"] = _big_rows("short_context", 1)

    kept = _kept(context, _floor_budget(context))

    surviving = [k for k in _DROP_ORDER if _holds_droppable(kept, k)]
    indices = [_DROP_ORDER.index(k) for k in surviving]
    assert indices == list(range(indices[0], len(_DROP_ORDER))), (
        f"还留着可丢行的块 {surviving} 不是裁剪序的后缀——有低优先块抢在了高优先块之前")
    assert not [r for r in kept["long_term_facts"] if not r.get("is_hard")], \
        "drop_order 最小的非 hard 事实必须先让位"


def test_never_drops_hard_facts_or_short_context():
    """is_hard 事实与 short_context 一行不掉——可丢块全抽干也不动它们。"""
    context = {
        "long_term_facts": _big_rows("hard", 2, hard=True) + _big_rows("soft", 8),
        "mid_term_events": _big_rows("event", 8),
        "short_context": _big_rows("short", 2),
    }
    kept = _kept(context, _required_only_budget(context))
    assert len(kept["long_term_facts"]) == 2, "非 hard 事实应被抽干，hard 事实留下"
    assert all(r["is_hard"] for r in kept["long_term_facts"])
    assert len(kept["short_context"]) == 2
    assert kept["mid_term_events"] == []
    assert len(context["long_term_facts"]) == 10, "fit_prompt 只复制，不得就地改入参"
