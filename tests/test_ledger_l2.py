"""正文-台账语义比对 L2 测试（§8.6 阶段 3 长线治理切片 4，conflict-samples.md 样例 16/17/20/21/22/24/25）。

mock LLM（validator_l2，不依赖真实 DeepSeek key）。验证：
- 确定性预滤：只判「old_value == 台账当前值 且 new_value ≠ 台账当前值」候选；判定集空 → 零 LLM 调用；
- 三类矛盾检出（样例 16/20/21 状态、17/22 关系）→ 确定性严重度/作用域、conflict_key 稳定、evidence 逐字；
- 阴性 0 误报：合法演化（样例 24/25）verdict=valid；守卫丢弃伪造 key / 低置信 / 非逐字证据；
- 接线：node_validate 并入 summary["l2_major"] 与 unresolved；node_audit 合并不覆盖（D3）；
  route_after_audit l2_major → revise / 预算耗尽 needs_review；node_persist l2_major → 待确认池；
- LLM/解析失败非阻断。

桩设计（test_bridge_audit 模板）：LLM 单点注入 = monkeypatch `myink.validation.ledger_l2.make_chain`。
"""

from __future__ import annotations

import json
import uuid

import pytest

from myink.db import tenant_session
from myink.models import Chapter, CharacterState, MemoryCandidate, Relation
from myink.providers.base import ModelResponse
from myink.schemas import Finding
from myink.validation import ledger_l2 as l2
from myink.validation.l1 import _key
from myink.workflow import nodes
from myink.workflow.chapter_graph import route_after_audit

REALM_ORDER = ["炼气", "筑基", "金丹", "元婴", "化神", "大乘", "渡劫"]
A, B, C, D = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

# 通用台账字段（镜像 _GENERAL_STATE_FIELDS）
FIELDS = ["injury", "location", "goal", "identity", "power", "item", "knowledge"]
_STATE_VALUES = {"injury": "轻伤", "location": "东海水宫", "goal": "查明玉佩真相", "identity": "散修",
                 "power": "筑基后期", "item": "青玉匣", "knowledge": "未知禁地"}

DRAFT16 = "林砚负手立于北境城楼，气色如常，与守将谈笑风生。第7章。"  # 样例 16：无治疗/移动过渡
DRAFT24 = "林砚服下回春丹，闭关三日，伤势尽复。第7章。"  # 样例 24：先建立治疗（合法）


# ---- 种子辅助（temp_project 不带角色/台账，必须自种；镜像 test_state_relation_checks）----


def _seed_state(pid: str, **kw):
    fields = {"character_id": A, "chapter_seq": 5, "field": "injury",
              "old_value": None, "new_value": "濒死", "source_chapter": 5}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(CharacterState(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _seed_relation(pid: str, **kw):
    fields = {"source_id": A, "relation_type": "hostile", "target_id": B, "source_chapter": 3}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(Relation(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _state_cand(field: str, old_v, new_v, cid: uuid.UUID | None = None, chapter: int = 7) -> dict:
    return {"kind": "character_state", "source_chapter": chapter, "confidence": 0.9,
            "payload": {"character_id": str(cid or A), "field": field,
                        "old_value": old_v, "new_value": new_v}}


def _rel_cand(rtype: str, old_v, new_v, src: uuid.UUID | None = None,
              tgt: uuid.UUID | None = None, chapter: int = 7) -> dict:
    return {"kind": "relation_change", "source_chapter": chapter, "confidence": 0.9,
            "payload": {"source_id": str(src or A), "target_id": str(tgt or B),
                        "relation_type": rtype, "old_value": old_v, "new_value": new_v}}


# ---- LLM 桩（单点：myink.validation.ledger_l2.make_chain）----


class _Chain:
    def __init__(self, provider):
        self.provider = provider

    def generate(self, messages, *, json_mode=False, max_tokens=None, temperature=None, tools=None):
        return self.provider.generate(messages, model_id="stub", max_tokens=max_tokens,
                                      temperature=temperature, json_mode=json_mode, tools=tools)


class _LedgerStub:
    """validator_l2 stub：返回预置 judgments（或 error），记录调用数与消息。"""

    def __init__(self, judgments=None, error=None):
        self.judgments = judgments if judgments is not None else []
        self.error = error
        self.calls = 0
        self.messages = None

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        self.calls += 1
        self.messages = messages
        if self.error:
            return ModelResponse(content="", model_id=model_id, error=self.error,
                                 input_tokens=50, output_tokens=0, duration_ms=30)
        return ModelResponse(content=json.dumps({"judgments": self.judgments}, ensure_ascii=False),
                             model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)


@pytest.fixture
def install_stub(monkeypatch):
    """注入单点：patch myink.validation.ledger_l2.make_chain（正文-台账语义比对唯一桩点）。"""

    def _install(stub):
        monkeypatch.setattr(l2, "make_chain", lambda role, **_kwargs: _Chain(stub))

    return _install


def _judgment(key, verdict="invalid", evidence="", confidence=0.9):
    return {"key": key, "verdict": verdict, "evidence": evidence, "reason": "test", "confidence": confidence}


def _run_l2(pid, candidates, draft, chapter_seq=7):
    with tenant_session(pid) as db:
        return l2.run_ledger_l2(db, project_id=uuid.UUID(pid), chapter_seq=chapter_seq,
                                candidates=candidates, draft=draft)


# ---- A 组 · 判定集预滤短路 ----


def test_judgment_set_empty_zero_llm_no_candidates(temp_project, install_stub):
    """无候选 → 判定集空 → 零 LLM 调用、零 finding。"""
    stub = _LedgerStub()
    install_stub(stub)
    assert _run_l2(temp_project, [], DRAFT16) == []
    assert stub.calls == 0


def test_judgment_set_empty_old_mismatch_ledger(temp_project, install_stub):
    """old ≠ 台账（L1 域：抽取误读）→ 不进判定集 → 零 LLM。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    stub = _LedgerStub()
    install_stub(stub)
    # old=重伤 与台账 濒死 不符 → L1 抓，L2 短路
    assert _run_l2(temp_project, [_state_cand("injury", "重伤", "如常")], DRAFT16) == []
    assert stub.calls == 0


def test_judgment_set_empty_realm_excluded(temp_project, install_stub):
    """realm 有专属 L1 检查 → 不进判定集（防双报）。"""
    _seed_state(temp_project, field="realm", new_value="筑基后期")
    stub = _LedgerStub()
    install_stub(stub)
    assert _run_l2(temp_project, [_state_cand("realm", "筑基后期", "金丹")], DRAFT16) == []
    assert stub.calls == 0


# ---- A 组 · 三类矛盾检出（样例 16/20/21/17/22）----


def test_positive_sample16_injury_flip(temp_project, install_stub):
    """样例 16：injury 濒死→如常 无过渡 → character_state/major/local、key 稳定、evidence 逐字。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    stub = _LedgerStub(judgments=[_judgment(key, evidence="林砚负手立于北境城楼")])
    install_stub(stub)
    got = _run_l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16)
    assert len(got) == 1 and stub.calls == 1
    f = got[0]
    assert f["conflict_type"] == "character_state" and f["severity"] == "major" and f["scope"] == "local"
    assert f["source"] == "L2" and f["conflict_key"] == key
    assert f["evidence"] == [{"chapter": 7, "quote": "林砚负手立于北境城楼"}]


def test_positive_sample20_goal_flip(temp_project, install_stub):
    """样例 20：goal 无过渡推翻 → character_state/minor/local。"""
    _seed_state(temp_project, field="goal", new_value="查明玉佩真相")
    key = _key("character_state", f"{str(A)}:goal", 7)
    stub = _LedgerStub(judgments=[_judgment(key, evidence="林砚已彻底放下玉佩一事")])
    install_stub(stub)
    draft = "林砚已彻底放下玉佩一事，将全副心思投在黑市商号上。第7章。"
    got = _run_l2(temp_project, [_state_cand("goal", "查明玉佩真相", "放下玉佩")], draft)
    assert len(got) == 1
    assert got[0]["severity"] == "minor" and got[0]["scope"] == "local"


def test_positive_sample21_identity_no_source(temp_project, install_stub):
    """样例 21：identity 散修→城主 无来源 → character_state/major/local。"""
    _seed_state(temp_project, field="identity", new_value="散修")
    key = _key("character_state", f"{str(A)}:identity", 7)
    stub = _LedgerStub(judgments=[_judgment(key, evidence="林砚以城主身份坐镇北境城")])
    install_stub(stub)
    draft = "林砚以城主身份坐镇北境城，点将校尉，发号施令。第7章。"
    got = _run_l2(temp_project, [_state_cand("identity", "散修", "城主")], draft)
    assert len(got) == 1
    assert got[0]["severity"] == "major" and got[0]["scope"] == "local"


def test_positive_relation_sample17(temp_project, install_stub):
    """样例 17：relation hostile→ally 无变更记录 → relation/major/structural。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=6)
    key = _key("relation", f"{str(A)}:{str(B)}", 7)
    stub = _LedgerStub(judgments=[_judgment(key, evidence="蛟王与林砚并肩立于水宫殿前")])
    install_stub(stub)
    draft = "蛟王与林砚并肩立于水宫殿前，道：\"林兄，今日之事有劳了。\"第7章。"
    got = _run_l2(temp_project, [_rel_cand("ally", "hostile", "ally")], draft)
    assert len(got) == 1
    f = got[0]
    assert f["conflict_type"] == "relation" and f["severity"] == "major" and f["scope"] == "structural"
    assert f["conflict_key"] == key


def test_positive_relation_sample22(temp_project, install_stub):
    """样例 22：relation master_student→hostile → relation/major/structural。"""
    _seed_relation(temp_project, source_id=A, relation_type="master_student", target_id=B, source_chapter=2)
    stub = _LedgerStub(judgments=[_judgment(_key("relation", f"{str(A)}:{str(B)}", 7), evidence="沈沧澜立于林砚对面，冷笑")])
    install_stub(stub)
    draft = "沈沧澜立于林砚对面，冷笑：\"逆徒，今日便取你性命。\"第7章。"
    got = _run_l2(temp_project, [_rel_cand("hostile", "master_student", "hostile")], draft)
    assert len(got) == 1
    assert got[0]["conflict_type"] == "relation" and got[0]["severity"] == "major"


# ---- A 组 · 阴性合法演化（样例 24/25）----


def test_negative_sample24_valid_transition(temp_project, install_stub):
    """样例 24：先有服回春丹闭关 → verdict=valid → 0 finding。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    stub = _LedgerStub(judgments=[_judgment(key, verdict="valid", evidence="林砚服下回春丹闭关三日")])
    install_stub(stub)
    got = _run_l2(temp_project, [_state_cand("injury", "濒死", "痊愈")], DRAFT24)
    assert got == [] and stub.calls == 1


def test_negative_sample25_valid_reconciliation(temp_project, install_stub):
    """样例 25：hostile→ally 有把酒言和 → verdict=valid → 0 finding。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=6)
    key = _key("relation", f"{str(A)}:{str(B)}", 7)
    stub = _LedgerStub(judgments=[_judgment(key, verdict="valid", evidence="把酒言和")])
    install_stub(stub)
    draft = "林砚与蛟王把酒言和，结下三月盟约。第7章。"
    got = _run_l2(temp_project, [_rel_cand("ally", "hostile", "ally")], draft)
    assert got == [] and stub.calls == 1


# ---- A 组 · 本地守卫（0 误报兜底）----


def test_guard_drops_bad_evidence(temp_project, install_stub):
    """evidence 非 draft 逐字子串（改写/编造）→ 丢弃。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    stub = _LedgerStub(judgments=[_judgment(key, evidence="正文中不存在的句子")])
    install_stub(stub)
    assert _run_l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16) == []


def test_guard_drops_low_confidence(temp_project, install_stub):
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    stub = _LedgerStub(judgments=[_judgment(key, evidence="林砚负手立于北境城楼", confidence=0.4)])
    install_stub(stub)
    assert _run_l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16) == []


def test_guard_drops_wrong_verdict(temp_project, install_stub):
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    stub = _LedgerStub(judgments=[_judgment(key, verdict="valid", evidence="林砚负手立于北境城楼")])
    install_stub(stub)
    assert _run_l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16) == []


def test_guard_drops_unknown_key(temp_project, install_stub):
    _seed_state(temp_project, field="injury", new_value="濒死")
    stub = _LedgerStub(judgments=[_judgment("deadbeef00000000", evidence="林砚负手立于北境城楼")])
    install_stub(stub)
    assert _run_l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16) == []


# ---- A 组 · 失败非阻断 / cap / 消息组装 ----


def test_llm_failure_non_blocking(temp_project, install_stub):
    _seed_state(temp_project, field="injury", new_value="濒死")
    stub = _LedgerStub(error="validator_l2 不可用")
    install_stub(stub)
    assert _run_l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16) == []
    assert stub.calls == 1


def test_cap_8_judgments(temp_project, install_stub):
    """10 个判定候选（7 状态 + 3 关系）→ cap 8，LLM 只收 8 项。"""
    for field, v in _STATE_VALUES.items():
        _seed_state(temp_project, field=field, new_value=v)
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="ally", target_id=C, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="master_student", target_id=D, source_chapter=3)
    cands = [_state_cand(f, v, "新值") for f, v in _STATE_VALUES.items()]
    cands += [_rel_cand("ally", "hostile", "ally", tgt=B), _rel_cand("hostile", "ally", "敌", tgt=C),
              _rel_cand("hostile", "master_student", "hostile", tgt=D)]
    stub = _LedgerStub()
    install_stub(stub)
    assert _run_l2(temp_project, cands, DRAFT16) == []
    assert stub.calls == 1
    user = stub.messages[1]["content"]
    assert user.count("\n- [") == 8, f"cap=8 应只发 8 项，实际 {user.count(chr(10) + '- [')}"


def test_messages_assembled(temp_project, install_stub):
    """消息组装：user 含正文 + 判定项（实体/字段/台账当前值/候选新值）。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    stub = _LedgerStub()
    install_stub(stub)
    _run_l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16)
    user = stub.messages[1]["content"]
    assert "林砚负手立于北境城楼" in user
    assert "状态变更" in user and "injury" in user and "台账=濒死" in user and "候选新值=如常" in user


# ---- B 组 · 接线与路由 ----


def test_node_validate_merges_l2_major(temp_project, install_stub):
    """node_validate 集成：L2 major 并入 summary["l2_major"] 与 unresolved；L1 minor 不在 unresolved。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    stub = _LedgerStub(judgments=[_judgment(key, evidence="林砚负手立于北境城楼")])
    install_stub(stub)
    state = {"project_id": temp_project, "chapter_seq": 7,
             "settings": {"realm_order": REALM_ORDER},
             "candidates": [_state_cand("injury", "濒死", "如常")], "draft": DRAFT16}
    out = nodes.node_validate(state)
    report = out["report"]
    assert report["summary"].get("l2_major") == 1
    assert report["summary"].get("total", 0) >= 1
    l2_in_unresolved = [f for f in out["unresolved"] if f.get("source") == "L2"]
    assert len(l2_in_unresolved) == 1 and l2_in_unresolved[0]["conflict_key"] == key
    # 同候选 L1 不双报（old==台账 → L1 跳过）
    assert all(f.get("source") != "L1" or f.get("conflict_key") != key for f in out["unresolved"])


def test_merge_unresolved_keeps_l1_l2_and_overrides_same_key():
    """_merge_unresolved：L1/L2 不同键共存；同键以 audit 判定覆盖。"""
    l2_f = {"conflict_key": "k1", "severity": "major", "source": "L2"}
    l1_f = {"conflict_key": "k2", "severity": "major", "source": "L1"}
    audit_override = Finding(conflict_key="k1", conflict_type="character_state", severity="major",
                             scope="local", source="L2", evidence=[{"chapter": 7, "quote": "q"}])
    audit_new = Finding(conflict_key="k3", conflict_type="relation", severity="major",
                        scope="structural", source="L2", evidence=[{"chapter": 7, "quote": "q"}])
    merged = nodes._merge_unresolved([l2_f, l1_f], [audit_override, audit_new])
    keys = {f["conflict_key"] for f in merged}
    assert keys == {"k1", "k2", "k3"}  # L1/L2 不同键共存
    by_key = {f["conflict_key"]: f for f in merged}
    assert by_key["k1"].get("scope") == "local"  # 同键被 audit 判定覆盖


def test_route_after_audit_l2_major_routing():
    """route_after_audit：l2_major → revise；预算耗尽 → needs_review；无 → 原 pass 路径。"""
    from myink.config import settings as s
    base = {"report": {"summary": {"critical": 0}}, "revision_count": 0, "replan_count": 0,
            "audit_verdict": {"verdict": "pass"}}
    # 无 l2_major → pass 路径放行
    assert route_after_audit(base) == "persist"
    # l2_major=1 → revise（可修）
    state = dict(base, report={"summary": {"critical": 0, "l2_major": 1}})
    assert route_after_audit(state) == "revise"
    # l2_major + 预算耗尽 → needs_review（persist 按 l2_major 分流）
    state = dict(state, revision_count=s.max_revisions)
    assert route_after_audit(state) == "needs_review"
    # l1_critical 语义不变
    assert route_after_audit(dict(base, report={"summary": {"critical": 1}})) == "revise"


def test_node_persist_l2_major_pools_candidates(temp_project):
    """node_persist：critical==0 且 l2_major>0 → 候选进待确认池 + 章节 awaiting_review（不 auto 落库）。"""
    cand = _state_cand("injury", "濒死", "如常")
    state = {"project_id": temp_project, "chapter_seq": 7, "draft": DRAFT16,
             "candidates": [cand], "report": {"summary": {"critical": 0, "l2_major": 1}},
             "task_id": None}
    out = nodes.node_persist(state)
    assert out["persisted"] is True and out["needs_review"] is True
    with tenant_session(temp_project) as db:
        pool = db.query(MemoryCandidate).filter(
            MemoryCandidate.project_id == uuid.UUID(temp_project)).all()
        assert len(pool) == 1 and pool[0].kind == "character_state"
        ch = db.query(Chapter).filter(
            Chapter.project_id == uuid.UUID(temp_project), Chapter.chapter_seq == 7).first()
        assert ch is not None and ch.status == "awaiting_review"
