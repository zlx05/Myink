"""节点级自修复重试（§6.12 调用层兜底第 4 档）：非法 JSON / 空正文 / 跑偏时重发 + 修正指令。

用户实测（2026-08-22）：`plan_chapter 输出解析失败`、`DeepSeek 返回空白/空内容` 把整章
一次判死。补 `_llm_checked`：check(content) 抛异常 → 追加 system 修正指令重发（默认重试
2 次）；resp.error 直接透传不重试（降级链已用尽，且 test_flow BatchFailStub 语义不能被吃）。

模式：monkeypatch `nodes._llm`（test_autocard 同款桩），返回坏→好序列，验证重试机制；
再经真实 `node_plan_chapter`（temp_project）验证节点级重试落回 {"plan": ...}。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from myink.workflow import nodes

_PLAN = {
    "goals": ["查明玉佩异动"],
    "scenes": [],
    "characters": [],
    "hooks_to_plant": [],
    "hooks_to_resolve": [],
    "expected_events": ["玉佩示警"],
    "hard_constraints": [],
}


def _queued_llm(monkeypatch, contents):
    """mock nodes._llm：按队列依次返回 content（或 Exception → resp.error），记录次数与 messages。

    content 与 _llm 桩的 __getitem__ 对齐——第 1 次取 contents[0]，重试取后续，越界报错
    （测「不该重试却重试」）。
    """
    calls = {"n": 0, "messages": []}

    def fake_llm(db, state, node, role, chain, messages, **kw):
        calls["n"] += 1
        calls["messages"].append(messages)
        item = contents[calls["n"] - 1]
        if isinstance(item, Exception):
            resp = SimpleNamespace(error=str(item), content="")
        else:
            resp = SimpleNamespace(error=None, content=item)
        return resp, []

    monkeypatch.setattr(nodes, "_llm", fake_llm)
    return calls


def test_llm_checked_retries_parse_failure_then_succeeds(monkeypatch):
    """解析失败 → 第 2 次带修正指令重发 → 成功返回（一次坏输出不再判死整章）。"""
    calls = _queued_llm(monkeypatch, ["not json{{", json.dumps(_PLAN, ensure_ascii=False)])

    def _check(content):
        return json.loads(content)["goals"]

    _, _, result, err = nodes._llm_checked(
        None, {"project_id": "x"}, "plan_chapter", "Planner", None,
        [{"role": "user", "content": "u"}], check=_check)
    assert err is None and result == _PLAN["goals"]
    assert calls["n"] == 2
    assert calls["messages"][1][-1]["role"] == "system"
    assert "未通过 JSON" in calls["messages"][1][-1]["content"], "重试应追加 JSON 修正指令"
    assert calls["messages"][1][-2] == {"role": "assistant", "content": "not json{{"}, \
        "JSON 重试必须带回上一版错误输出，才能定点修复而不是从头重复规划"


def test_llm_checked_all_fail_returns_error_at_limit(monkeypatch):
    """始终解析失败 → 初始 + 重试 max_attempts 次后 error 带次数与末次 content 片段。"""
    calls = _queued_llm(monkeypatch, ["bad{{{", "worse{{{", "nope"])

    def _check_json(content):
        return json.loads(content)["ok"]  # 三个输出都非法 → 每次都抛

    _, _, result, err = nodes._llm_checked(
        None, {"project_id": "x"}, "write", "Writer", None,
        [{"role": "user", "content": "u"}], check=_check_json,
        json_mode=False, corrective="请重输出正文。")
    assert result is None
    assert err is not None and "write 输出多次不合格（已重试 2 次）" in err
    assert "content[:120]=" in err, "最终失败应带末次 content 片段供排查"
    assert calls["n"] == 3, "初始 1 次 + 重试 2 次（_LLM_CHECK_RETRIES=2）"


def test_llm_checked_passthrough_resp_error_no_retry(monkeypatch):
    """resp.error 直接透传、不重试（降级链已用尽；BatchFailStub「失败一次→批次失败」语义不被吃掉）。"""
    calls = _queued_llm(monkeypatch, [RuntimeError("provider down")])
    _, _, result, err = nodes._llm_checked(
        None, {"project_id": "x"}, "plan_chapter", "Planner", None,
        [{"role": "user", "content": "u"}], check=lambda c: c)
    assert err == "provider down" and result is None
    assert calls["n"] == 1, "resp.error 不应触发节点级重试"


def test_llm_checked_text_mode_default_corrective(monkeypatch):
    """json_mode=False（write/revise 正文路径）默认用文本修正指令，且空正文触发重试。"""
    calls = _queued_llm(monkeypatch, ["", "=== CONTENT ===\n正文"])
    _, _, draft, err = nodes._llm_checked(
        None, {"project_id": "x"}, "write", "Writer", None,
        [{"role": "user", "content": "u"}], check=nodes._check_draft,
        json_mode=False, disable_thinking=True)
    assert err is None and draft == "正文"
    assert calls["n"] == 2
    assert "纯文本散文" in calls["messages"][1][-1]["content"], "文本路径默认用 _RETRY_CORRECTIVE_TEXT"


def test_llm_checked_text_noise_triggers_retry(monkeypatch):
    """正文跑偏（工具调用文本）→ 判噪声重试（_check_draft 的显式失败是可重试的）。"""
    calls = _queued_llm(monkeypatch,
                        ["=== CONTENT ===\n<invoke name=\"inspect_facts\">",
                         "=== CONTENT ===\n夜色沉沉，林砚推门而出。"])
    _, _, draft, err = nodes._llm_checked(
        None, {"project_id": "x"}, "write", "Writer", None,
        [{"role": "user", "content": "u"}], check=nodes._check_draft,
        json_mode=False, disable_thinking=True)
    assert err is None and draft == "夜色沉沉，林砚推门而出。"
    assert calls["n"] == 2


def test_node_plan_chapter_retries_bad_json(temp_project, monkeypatch):
    """真实节点：plan_chapter 坏 JSON → _llm_checked 重发 → 落回 {"plan": ...}（chapter_seq 补全）。"""
    calls = _queued_llm(monkeypatch, ["not json{{", json.dumps(_PLAN, ensure_ascii=False)])
    out = nodes.node_plan_chapter({"project_id": temp_project, "chapter_seq": 1, "context": {}})
    assert "error" not in out, out.get("error")
    assert out["plan"]["chapter_seq"] == 1
    assert out["plan"]["goals"] == _PLAN["goals"]
    assert calls["n"] == 2, "坏 JSON 应重发一次而不是直接判失败"


def test_node_plan_chapter_repairs_missing_comma_without_model_retry(temp_project, monkeypatch):
    """Planner 只漏一个结构逗号时本地修复，不能让用户看到整份 Plan 连续重写。"""
    malformed = json.dumps(_PLAN, ensure_ascii=False).replace('], "scenes"', '] "scenes"', 1)
    calls = _queued_llm(monkeypatch, [malformed])

    out = nodes.node_plan_chapter({"project_id": temp_project, "chapter_seq": 1, "context": {}})

    assert "error" not in out, out.get("error")
    assert out["plan"]["goals"] == _PLAN["goals"]
    assert calls["n"] == 1, "可确定修复的漏逗号不应再次调用 Planner"


def test_node_plan_chapter_repairs_inexact_anchor_without_model_retry(temp_project, monkeypatch):
    """生成计划误引或改写章尾时只替换证据字段，不重新规划场景。"""
    tail = "众人收好物资。\n桥下忽然传来三声短促的敲击，林眠抬手示意所有人噤声。"
    plan = {
        **_PLAN,
        "transition": {
            "mode": "continue",
            "anchor_quote": "桥下传来敲击声，大家安静下来",
            "pending_action": "查明桥下敲击来源",
            "opening_beat": "林眠靠近桥栏辨认声音方位",
            "bridge": "",
        },
    }
    calls = _queued_llm(monkeypatch, [json.dumps(plan, ensure_ascii=False)])

    out = nodes.node_plan_chapter({
        "project_id": temp_project,
        "chapter_seq": 2,
        "context": {"short_context": [{"kind": "prev_chapter_tail", "chapter": 1, "tail": tail}]},
    })

    assert "error" not in out, out.get("error")
    assert out["plan"]["transition"]["anchor_quote"] == tail.splitlines()[-1]
    assert out["plan"]["scenes"] == _PLAN["scenes"]
    assert calls["n"] == 1, "可从章尾确定性纠正的引文不应重跑 Planner"
