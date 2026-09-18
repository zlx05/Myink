"""状态/关系一致性 · 确定性奠基（conflict-samples.md 样例 16/17/20/21/22 台账侧，§7.7/§7.8）。

三个交付物，全部确定性、无模型：
- persist 关系关闭修复：relation_change 落库前关闭同 (source,target) 有序对全部活跃旧行、
  透传 payload valid_to（schema.md §6「当前关系 = 最新 valid_to IS NULL」，样例 25 临时盟约）；
- L1 关系台账自洽：重复活跃 / 矛盾活跃行 → major（样例 17/22 台账侧 + 新增样例）；
- L1 候选 old_value-vs-台账：extract 误读注入快照 → minor（样例 16/20/21 确定性窄脚印；
  正文-台账语义比对属 L2，明确不改）。

阴性必须 0 检出：单行活跃关系、空表、候选 old_value 相符、台账无值首写。
"""

from __future__ import annotations

import uuid

from myink.db import tenant_session
from myink.models import CharacterState, Relation
from myink.schemas import MutationCandidate
from myink.validation.l1 import L1Validator
from myink.workflow import nodes

REALM_ORDER = ["炼气", "筑基", "金丹", "元婴", "化神", "大乘", "渡劫"]

A, B, C = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


# ---- 辅助 ----

def _validate(pid: str, chapter_seq: int, candidates: list[MutationCandidate] | None = None) -> list[dict]:
    with tenant_session(pid) as db:
        findings = L1Validator(REALM_ORDER).validate(
            db, project_id=uuid.UUID(pid), chapter_seq=chapter_seq,
            candidates=candidates or [],
        )
        return [f.model_dump(mode="json") for f in findings]


def _seed_relation(pid: str, **kw):
    fields = {"source_id": A, "relation_type": "hostile", "target_id": B, "source_chapter": 1}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(Relation(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _seed_state(pid: str, **kw):
    fields = {"character_id": A, "chapter_seq": 5, "field": "goal",
              "old_value": None, "new_value": "查明玉佩真相", "source_chapter": 5}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(CharacterState(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _all_relations(pid: str) -> list[Relation]:
    with tenant_session(pid) as db:
        return db.query(Relation).filter(Relation.project_id == uuid.UUID(pid)).all()


def _rel_cand(src, tgt, rtype: str, chapter: int = 5, **extra) -> dict:
    payload = {"source_id": str(src), "target_id": str(tgt), "relation_type": rtype}
    payload.update(extra)
    return {"kind": "relation_change", "source_chapter": chapter, "payload": payload, "confidence": 0.9}


def _state_cand(field: str, old_v, new_v, chapter: int = 7) -> MutationCandidate:
    return MutationCandidate(
        kind="character_state", source_chapter=chapter,
        payload={"character_id": str(A), "field": field,
                 "old_value": old_v, "new_value": new_v},
        confidence=0.9,
    )


# ---- persist 关闭修复 ----

def test_persist_relation_change_closes_prior_active_row(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 5, [_rel_cand(A, B, "ally")])
        db.flush()
    rows = _all_relations(temp_project)
    hostile = [r for r in rows if r.relation_type == "hostile"]
    assert len(hostile) == 1 and hostile[0].valid_to == 5  # 旧行已关闭
    active = [r for r in rows if r.valid_to is None]
    assert len(active) == 1 and active[0].relation_type == "ally"  # 每对至多一条活跃
    assert not [f for f in _validate(temp_project, 6) if f["conflict_type"] == "relation"]


def test_persist_relation_change_honors_payload_valid_to(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 5, [_rel_cand(A, B, "ally", valid_to=16)])
        db.flush()
    rows = {r.relation_type: r for r in _all_relations(temp_project)}
    assert rows["hostile"].valid_to == 5
    assert rows["ally"].valid_to == 16  # 临时盟约透传（样例 25）


def test_persist_relation_change_only_closes_same_ordered_pair(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=2)
    _seed_relation(temp_project, source_id=B, relation_type="ally", target_id=A, source_chapter=2)
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 5, [_rel_cand(A, B, "ally")])
        db.flush()
    rows = _all_relations(temp_project)
    ab = [r for r in rows if r.source_id == A and r.target_id == B]
    assert {r.relation_type for r in ab if r.valid_to is None} == {"ally"}
    assert [r for r in ab if r.relation_type == "hostile"][0].valid_to == 5
    ba = [r for r in rows if r.source_id == B and r.target_id == A]
    assert [r for r in ba if r.relation_type == "ally"][0].valid_to is None  # 反向对不受影响


def test_persist_relation_change_incomplete_payload_skipped(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    bad = {"kind": "relation_change", "source_chapter": 5,
           "payload": {"source_id": str(A), "relation_type": "ally"}, "confidence": 0.9}
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 5, [bad])  # 缺 target → 不崩
        db.flush()
    rows = _all_relations(temp_project)
    assert len(rows) == 1 and rows[0].relation_type == "hostile" and rows[0].valid_to is None


def test_persist_relation_change_missing_type_skipped(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    bad = _rel_cand(A, B, None)
    del bad["payload"]["relation_type"]
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 5, [bad])  # 缺 type → 不崩
        db.flush()
    rows = _all_relations(temp_project)
    assert len(rows) == 1 and rows[0].valid_to is None


# ---- L1 关系台账自洽 ----

def test_l1_relation_ledger_duplicate_active_fires(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=4)
    fs = _validate(temp_project, 6)
    rel = [f for f in fs if f["conflict_type"] == "relation"]
    assert len(rel) == 1, fs
    assert rel[0]["severity"] == "major" and rel[0]["scope"] == "structural"
    assert "不可判定" in rel[0]["evidence"][0]["quote"]


def test_l1_relation_ledger_contradiction_active_fires(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="ally", target_id=B, source_chapter=4)
    fs = _validate(temp_project, 6)
    rel = [f for f in fs if f["conflict_type"] == "relation"]
    assert len(rel) == 1, fs
    assert rel[0]["severity"] == "major" and "矛盾" in rel[0]["evidence"][0]["quote"]


def test_l1_relation_ledger_both_dup_and_contradiction(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=4)
    _seed_relation(temp_project, source_id=A, relation_type="ally", target_id=B, source_chapter=5)
    fs = _validate(temp_project, 6)
    rel = [f for f in fs if f["conflict_type"] == "relation"]
    assert len(rel) == 2, fs
    assert len({f["conflict_key"] for f in rel}) == 2  # dup/con key 不冲突


def test_l1_relation_ledger_clean_single_row_no_fire(temp_project):
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    fs = _validate(temp_project, 6)
    assert not [f for f in fs if f["conflict_type"] == "relation"], fs


def test_l1_relation_ledger_empty_no_fire(temp_project):
    fs = _validate(temp_project, 6)  # demo 关系表为空 → 0 误报
    assert not [f for f in fs if f["conflict_type"] == "relation"], fs


# ---- L1 候选 old_value-vs-台账 ----

def test_l1_candidate_old_value_mismatch_fires(temp_project):
    _seed_state(temp_project, chapter_seq=5, field="goal", new_value="查明玉佩真相")
    fs = _validate(temp_project, 7, [_state_cand("goal", "追寻万魔渊", "查明玉佩真相")])
    cs = [f for f in fs if f["conflict_type"] == "character_state"]
    assert len(cs) == 1, fs
    assert cs[0]["severity"] == "minor" and cs[0]["scope"] == "local"
    assert "注入快照" in cs[0]["evidence"][0]["quote"] and "goal" in cs[0]["evidence"][0]["quote"]


def test_l1_candidate_old_value_match_no_fire(temp_project):
    _seed_state(temp_project, chapter_seq=5, field="goal", new_value="查明玉佩真相")
    fs = _validate(temp_project, 7, [_state_cand("goal", "查明玉佩真相", "放下玉佩")])
    assert not [f for f in fs if f["conflict_type"] == "character_state"], fs


def test_l1_candidate_old_value_empty_ledger_skip(temp_project):
    # 台账无当前值（首写）→ 跳过，fresh 书 0 误报
    fs = _validate(temp_project, 7, [_state_cand("goal", "陈年目标", "新目标")])
    assert not [f for f in fs if f["conflict_type"] == "character_state"], fs


def test_l1_candidate_realm_not_double_fired(temp_project):
    # realm 由 _realm_checks 消费，通用检查类别级排除 → 不双报
    _seed_state(temp_project, chapter_seq=5, field="realm", new_value="金丹")
    fs = _validate(temp_project, 7, [_state_cand("realm", "筑基", "金丹")])
    assert not [f for f in fs if f["conflict_type"] == "character_state"], fs


# ---- L1 候选 relation_change old_value-vs-台账（样例 17/22 窄脚印；正文语义比对留 L2）----


def _rel_mc(rtype: str, old_v, new_v, src=A, tgt=B, chapter: int = 5) -> MutationCandidate:
    return MutationCandidate(
        kind="relation_change", source_chapter=chapter,
        payload={"source_id": str(src), "target_id": str(tgt), "relation_type": rtype,
                 "old_value": old_v, "new_value": new_v},
        confidence=0.9,
    )


def test_l1_relation_change_old_value_mismatch_fires(temp_project):
    """候选 old_value ≠ 台账当前有序对类型（抽取误读快照）→ minor/local。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    fs = _validate(temp_project, 7, [_rel_mc("hostile", "master_student", "hostile")])
    misread = [f for f in fs if "候选 old_value" in f["evidence"][0]["quote"]]
    assert len(misread) == 1, fs
    assert misread[0]["severity"] == "minor" and misread[0]["scope"] == "local"
    assert misread[0]["conflict_type"] == "relation"


def test_l1_relation_change_old_value_match_no_fire(temp_project):
    """候选 old_value == 台账 → 不报（正文-台账语义比对留 L2，样例 17 机制入口）。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    fs = _validate(temp_project, 7, [_rel_mc("ally", "hostile", "ally")])
    assert not [f for f in fs if "候选 old_value" in f["evidence"][0]["quote"]], fs


def test_l1_relation_change_ledger_missing_skip(temp_project):
    """台账该有序对无活跃行（首写）→ 跳过，fresh 书 0 误报。"""
    fs = _validate(temp_project, 7, [_rel_mc("ally", "hostile", "ally")])
    assert not [f for f in fs if "候选 old_value" in f["evidence"][0]["quote"]], fs


def test_l1_relation_change_ambiguity_skip(temp_project):
    """有序对 >1 活跃行（重复/矛盾）→ 当前类型不可判定 → 本检查跳过（relation_ledger_check 兜底）。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="ally", target_id=B, source_chapter=5)
    fs = _validate(temp_project, 7, [_rel_mc("hostile", "hostile", "ally")])
    # 窄脚印不误报；若 relation_ledger_check 报矛盾 major 属另一检查，与本检查无关
    assert not [f for f in fs if "候选 old_value" in f["evidence"][0]["quote"]], fs
