"""设定实体提示词块（§7.11 ④，审计报告第 5 项）。

覆盖两件事：
- `_render_setting` 的边界——白名单类型 / 白名单外历史值不抛错 / description=None；
- 【设定实体】块出现在 plan / write / audit 三处，**不出现在 extract**：extract 的
  【当前台账快照】是给 LLM 校准 character_state.old_value 与产出 relation_change 候选
  用的，掺入非人物行会招来追不上的假候选。
"""

from __future__ import annotations

from aiink.workflow.prompts import (
    _audit_messages,
    _extract_messages,
    _plan_messages,
    _render_setting,
    _write_messages,
)

ITEMS = [
    {"entity_id": "e1", "entity_type": "item", "name": "青锋剑",
     "description": "林砚佩剑，剑身有裂", "first_seen_chapter": 3},
    {"entity_id": "e2", "entity_type": "skill", "name": "九转炼体诀",
     "description": None, "first_seen_chapter": None},
    # entity_type 没有 DB CHECK（白名单只在写侧 new_entity），历史值必须能渲染
    {"entity_id": "e3", "entity_type": "神器", "name": "无名古镜",
     "description": "", "first_seen_chapter": 5},
]


def _ctx() -> dict:
    return {
        "long_term_facts": [], "mid_term_events": [], "short_context": [],
        "recent_openings": [], "entity_snapshots": [], "open_foreshadows": [],
        "plot_threads": [], "reflexions": [], "setting_snapshots": [dict(i) for i in ITEMS],
    }


def _text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


def test_render_setting_known_type():
    assert _render_setting(ITEMS[0]) == "- [物品/武器] 青锋剑（首见于第 3 章） 林砚佩剑，剑身有裂"


def test_render_setting_none_description_has_no_none_literal():
    line = _render_setting(ITEMS[1])
    assert line == "- [功法/技能] 九转炼体诀"
    assert "None" not in line


def test_render_setting_unknown_type_does_not_raise():
    assert _render_setting(ITEMS[2]) == "- [神器] 无名古镜（首见于第 5 章）"


def test_setting_block_in_plan_write_audit():
    ctx = _ctx()
    rendered = {
        "plan": _text(_plan_messages(dict(ctx))),
        "write": _text(_write_messages(dict(ctx), {"goals": ["夺回青锋剑"]})),
        "audit": _text(_audit_messages("正文", {}, dict(ctx), 4)),
    }
    for node, text in rendered.items():
        assert "【设定实体】" in text, node
        assert "青锋剑" in text, node


def test_setting_block_not_in_extract():
    text = _text(_extract_messages("正文", 4, _ctx()))
    assert "【设定实体】" not in text
    assert "青锋剑" not in text
    assert "无名古镜" not in text


def test_missing_setting_key_renders_empty_placeholder():
    """旧调用方 / 预算测试构造的 context 没有 setting_snapshots 键，不能 KeyError。"""
    ctx = _ctx()
    del ctx["setting_snapshots"]
    text = _text(_plan_messages(ctx))
    assert "【设定实体】" in text
    assert "（无）" in text
