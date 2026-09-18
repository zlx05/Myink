"""正文编辑 + 记忆增量校正（阶段 3 切片：编辑 → diff 变更集 → 确认池 → 删除候选失效）。

- 纯风格编辑（改措辞/标点）→ 变更集 keep 全量、池零新增（no_op，轻编辑零动作）；
- 删改剧情 → add 进池 / remove 以 memory_removal 候选进池，confirm 复用失效语义；
- apply_memory_removal 各类型幂等（事件硬删+向量、事实 expired+时间窗、状态/关系关窗、伏笔删）；
- confirm_candidate 分支处理删除候选；extract_candidates_from_draft 与节点共用抽取路径。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from myink.api.routes_chapters import ContentUpdate, correct_chapter_memory, delete_chapter, update_chapter_content
from myink.db import tenant_session
from myink.memory import correction
from myink.memory.correction import (apply_changeset_to_pool, apply_memory_removal,
                                     diff_changeset)
from myink.memory.vector_store import PgvectorStore
from myink.models import (Chapter, CharacterState, EmbeddingRow, Event, Fact,
                          Foreshadow, MemoryCandidate, Project, Relation)
from myink.providers.base import ModelProvider, ModelResponse
from myink.workflow import nodes

CID, CID2 = uuid.uuid4(), uuid.uuid4()
ZERO_VEC = [0.0] * 1024


# ---- extract stub（固定候选，仅测校正链路，不依赖真实 DeepSeek）----

class ExtractStub(ModelProvider):
    """extract 返回固定候选的假 provider。"""

    def name(self) -> str:
        return "stub-extract"

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        content = (
            '{"candidates":['
            '{"kind":"event","source_chapter":5,"confidence":0.9,"payload":{"summary":"林砚于黑市查探玉佩真相",'
            '"participants":["林砚"],"source_chapter":5,"confidence":0.9}},'
            '{"kind":"foreshadow","source_chapter":5,"confidence":0.9,"payload":{"description":"玉佩气息异常，疑似与玉佩真相有关",'
            '"trigger":{"actor":"林砚","action":"发现","object":"玉佩"},"source_chapter":5,"confidence":0.9}}'
            ']}'
        )
        return ModelResponse(content=content, model_id=model_id, input_tokens=100,
                             output_tokens=200, duration_ms=50)


@pytest.fixture
def extract_stub(monkeypatch):
    import myink.providers as providers_mod

    stub = ExtractStub()
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    return stub


# ---- 种子辅助 ----

def _cand(kind: str, payload: dict) -> dict:
    return {"kind": kind, "payload": payload, "confidence": 0.9}


def _seed_chapter(pid: str, seq: int, content: str) -> str:
    with tenant_session(pid) as db:
        ch = Chapter(project_id=uuid.UUID(pid), chapter_seq=seq, content=content,
                     status="confirmed", generation_source="auto")
        db.add(ch)
        db.commit()
        return str(ch.id)


def _seed_event(pid: str, seq: int, summary: str) -> str:
    with tenant_session(pid) as db:
        ev = Event(project_id=uuid.UUID(pid), summary=summary, source_chapter=seq, confidence=0.9)
        db.add(ev)
        db.flush()
        PgvectorStore().upsert(db, project_id=uuid.UUID(pid), level="event", source_id=ev.id,
                               source_chapter=seq, model_version="bge-m3", embedding=ZERO_VEC)
        db.commit()
        return str(ev.id)


def _seed_fact(pid: str, seq: int, content: str) -> str:
    with tenant_session(pid) as db:
        f = Fact(project_id=uuid.UUID(pid), content=content, category="关系", is_hard=False,
                 source_chapter=seq, confidence=0.9, confirm_status="confirmed")
        db.add(f)
        db.commit()
        return str(f.id)


def _seed_state(pid: str, seq: int) -> str:
    with tenant_session(pid) as db:
        s = CharacterState(project_id=uuid.UUID(pid), character_id=CID, chapter_seq=seq,
                           field="location", old_value=None, new_value="某地",
                           source_chapter=seq, confidence=0.9)
        db.add(s)
        db.commit()
        return str(s.id)


def _seed_relation(pid: str, seq: int) -> str:
    with tenant_session(pid) as db:
        r = Relation(project_id=uuid.UUID(pid), source_id=CID, relation_type="ally",
                     target_id=CID2, source_chapter=seq, confidence=0.9)
        db.add(r)
        db.commit()
        return str(r.id)


def _seed_foreshadow(pid: str, seq: int, description: str) -> str:
    with tenant_session(pid) as db:
        fo = Foreshadow(project_id=uuid.UUID(pid), description=description, status="planted",
                        planted_chapter=seq, trigger={})
        db.add(fo)
        db.commit()
        return str(fo.id)


# ---- diff 纯逻辑 ----

def test_diff_style_edit_keeps_all_no_churn():
    """纯风格编辑（措辞+标点改动）→ 语义不变，逐条命中 keep，零增删。"""
    persisted = [
        {"memory_type": "event", "memory_id": "e1", "kind": "event",
         "display": "林砚于黑市查探玉佩真相", "text": correction._sig("林砚于黑市查探玉佩真相")},
        {"memory_type": "foreshadow", "memory_id": "f1", "kind": "foreshadow",
         "display": "玉佩气息异常，疑似与玉佩真相有关", "text": correction._sig("玉佩气息异常，疑似与玉佩真相有关")},
    ]
    candidates = [
        _cand("event", {"summary": "林砚于黑市查探玉佩的真相。", "participants": ["林砚"]}),
        _cand("foreshadow", {"description": "玉佩气息异常，疑似与玉佩真相有关。", "trigger": {}}),
    ]
    cs = diff_changeset(persisted, candidates)
    assert cs["keep"] == 2 and cs["add"] == [] and cs["remove"] == []


def test_diff_removed_beat_becomes_removal():
    """删掉一个剧情句 → 对应事件不再被抽取 → removal。"""
    persisted = [
        {"memory_type": "event", "memory_id": "e1", "kind": "event",
         "display": "林砚于黑市查探玉佩真相", "text": correction._sig("林砚于黑市查探玉佩真相")},
        {"memory_type": "event", "memory_id": "e2", "kind": "event",
         "display": "林砚与旧识相遇", "text": correction._sig("林砚与旧识相遇")},
    ]
    candidates = [_cand("event", {"summary": "林砚于黑市查探玉佩真相", "participants": ["林砚"]})]
    cs = diff_changeset(persisted, candidates)
    assert cs["keep"] == 1 and cs["add"] == []
    assert [m["memory_id"] for m in cs["remove"]] == ["e2"]


def test_diff_new_plot_beat_added():
    """新增剧情句 → 新事件 add。"""
    persisted = [{"memory_type": "event", "memory_id": "e1", "kind": "event",
                  "display": "A", "text": correction._sig("A")}]
    candidates = [_cand("event", {"summary": "A", "participants": []}),
                  _cand("event", {"summary": "新增事件B", "participants": []})]
    cs = diff_changeset(persisted, candidates)
    assert cs["keep"] == 1 and len(cs["add"]) == 1 and cs["remove"] == []
    assert cs["add"][0]["payload"]["summary"] == "新增事件B"


def test_diff_character_state_exact_key():
    """结构化记忆按稳定键精确匹配：同 key keep，new_value 变化 → remove+add。"""
    persisted = [{"memory_type": "character_state", "memory_id": "s1", "kind": "character_state",
                  "display": "location: 某地",
                  "key": ("character_state", str(CID), "location", None, "某地"),
                  "text": correction._sig("某地")}]
    cs = diff_changeset(persisted, [_cand("character_state", {
        "character_id": str(CID), "field": "location", "old_value": None, "new_value": "某地"})])
    assert cs["keep"] == 1 and cs["add"] == [] and cs["remove"] == []
    cs2 = diff_changeset(persisted, [_cand("character_state", {
        "character_id": str(CID), "field": "location", "old_value": None, "new_value": "别处"})])
    assert cs2["keep"] == 0 and len(cs2["add"]) == 1 and len(cs2["remove"]) == 1


# ---- 池写入 ----

def test_apply_changeset_to_pool_idempotent(temp_project):
    """变更集 → 池：add 原样、remove 转 memory_removal；重跑不重复入池。"""
    pid = uuid.UUID(temp_project)
    cs = {
        "add": [_cand("event", {"summary": "新事件", "participants": []})],
        "remove": [{"memory_type": "fact", "memory_id": str(uuid.uuid4()), "display": "旧事实"}],
    }
    with tenant_session(temp_project) as db:
        stats = apply_changeset_to_pool(db, project_id=pid, chapter_seq=5, changeset=cs)
        db.flush()
        assert stats == {"add_to_pool": 1, "remove_to_pool": 1, "duplicates_skipped": 0}
        rows = db.query(MemoryCandidate).filter(MemoryCandidate.source_chapter == 5).all()
        assert {r.kind for r in rows} == {"event", "memory_removal"}
        rem = next(r for r in rows if r.kind == "memory_removal")
        assert rem.payload["memory_type"] == "fact"
        assert rem.payload["memory_id"] == cs["remove"][0]["memory_id"]
        stats2 = apply_changeset_to_pool(db, project_id=pid, chapter_seq=5, changeset=cs)
        db.flush()
        assert stats2 == {"add_to_pool": 0, "remove_to_pool": 0, "duplicates_skipped": 2}


# ---- apply_memory_removal（confirm 删除候选时按类型失效）----

def test_apply_memory_removal_event_deletes_embedding(temp_project):
    ev_id = _seed_event(temp_project, 5, "事件5")
    with tenant_session(temp_project) as db:
        ok = apply_memory_removal(db, project_id=uuid.UUID(temp_project),
                                  payload={"memory_type": "event", "memory_id": ev_id, "display": "x"},
                                  chapter_seq=5)
        db.flush()
        assert ok is True
        assert db.query(Event).filter(Event.id == uuid.UUID(ev_id)).count() == 0
        assert db.query(EmbeddingRow).filter(EmbeddingRow.source_id == uuid.UUID(ev_id)).count() == 0


def test_apply_memory_removal_fact_closes_expired(temp_project):
    fid = _seed_fact(temp_project, 5, "事实5")
    with tenant_session(temp_project) as db:
        ok = apply_memory_removal(db, project_id=uuid.UUID(temp_project),
                                  payload={"memory_type": "fact", "memory_id": fid, "display": "x"},
                                  chapter_seq=5)
        db.flush()
        f = db.get(Fact, uuid.UUID(fid))
        assert ok is True and f.valid_to == 5 and f.confirm_status == "expired"


def test_apply_memory_removal_state_and_relation_close(temp_project):
    rid = _seed_relation(temp_project, 5)
    sid = _seed_state(temp_project, 5)
    with tenant_session(temp_project) as db:
        apply_memory_removal(db, project_id=uuid.UUID(temp_project),
                             payload={"memory_type": "relation", "memory_id": rid}, chapter_seq=5)
        apply_memory_removal(db, project_id=uuid.UUID(temp_project),
                             payload={"memory_type": "character_state", "memory_id": sid}, chapter_seq=5)
        db.flush()
        assert db.get(Relation, uuid.UUID(rid)).valid_to == 5
        assert db.get(CharacterState, uuid.UUID(sid)).valid_to == 5


def test_apply_memory_removal_foreshadow_deletes(temp_project):
    fo_id = _seed_foreshadow(temp_project, 5, "伏笔5")
    with tenant_session(temp_project) as db:
        ok = apply_memory_removal(db, project_id=uuid.UUID(temp_project),
                                  payload={"memory_type": "foreshadow", "memory_id": fo_id}, chapter_seq=5)
        db.flush()
        assert ok is True
        assert db.query(Foreshadow).filter(Foreshadow.id == uuid.UUID(fo_id)).count() == 0


def test_apply_memory_removal_already_gone_idempotent(temp_project):
    """目标记忆已不存在（重写已删）→ 幂等成功，不报错。"""
    with tenant_session(temp_project) as db:
        ok = apply_memory_removal(db, project_id=uuid.UUID(temp_project),
                                  payload={"memory_type": "event", "memory_id": str(uuid.uuid4())},
                                  chapter_seq=5)
        db.flush()
        assert ok is True


# ---- confirm 分支 ----

def test_confirm_candidate_memory_removal_via_pool(temp_project):
    """池内 memory_removal 候选 confirm → 目标事实失效（expired + valid_to），候选标 confirmed。"""
    fid = _seed_fact(temp_project, 5, "待删事实")
    with tenant_session(temp_project) as db:
        cand = MemoryCandidate(project_id=uuid.UUID(temp_project), kind="memory_removal",
                               source_chapter=5,
                               payload={"memory_type": "fact", "memory_id": fid, "display": "待删事实"},
                               confidence=1.0)
        db.add(cand)
        db.flush()
        out = nodes.confirm_candidate(db, temp_project, cand.id)
        db.flush()
        assert out is not None and out.status == "confirmed"
        f = db.get(Fact, uuid.UUID(fid))
        assert f.valid_to == 5 and f.confirm_status == "expired"


# ---- extract helper 复用 ----

def test_extract_candidates_from_draft_shared_with_node(temp_project, extract_stub):
    with tenant_session(temp_project) as db:
        candidates, err = nodes.extract_candidates_from_draft(
            db, project_id=temp_project, chapter_seq=5, draft="正文", task_id=None)
        assert err is None
        kinds = {c["kind"] for c in candidates}
        assert {"event", "foreshadow"} <= kinds


# ---- 集成（API 端点，stub extract）----

def test_correct_memory_style_edit_noop(temp_project, extract_stub):
    """纯风格编辑后校正：正文与已落库记忆匹配 → no_op，池零新增。"""
    _seed_chapter(temp_project, 5, "林砚于黑市查探玉佩真相。玉佩气息异常。")
    _seed_event(temp_project, 5, "林砚于黑市查探玉佩真相")
    _seed_foreshadow(temp_project, 5, "玉佩气息异常，疑似与玉佩真相有关")
    report = correct_chapter_memory(temp_project, _find_chapter_id(temp_project, 5))
    assert report["no_op"] is True
    assert report["changeset"] == {"add": 0, "remove": 0, "keep": 2}
    with tenant_session(temp_project) as db:
        assert db.query(MemoryCandidate).filter(MemoryCandidate.source_chapter == 5).count() == 0


def test_correct_memory_removed_beat_produces_removal(temp_project, extract_stub):
    """删改剧情 → 未命中记忆产出 memory_removal 候选进池待确认。"""
    _seed_chapter(temp_project, 5, "林砚于黑市查探玉佩真相。玉佩气息异常。")
    _seed_event(temp_project, 5, "林砚于黑市查探玉佩真相")   # 匹配 stub → keep
    _seed_fact(temp_project, 5, "旧设定：玉佩来自昆仑")       # stub 不再产出 → removal
    report = correct_chapter_memory(temp_project, _find_chapter_id(temp_project, 5))
    assert report["no_op"] is False
    assert report["changeset"]["keep"] == 1
    assert report["changeset"]["remove"] == 1
    assert report["pool"]["remove_to_pool"] == 1
    with tenant_session(temp_project) as db:
        rem = db.query(MemoryCandidate).filter(MemoryCandidate.kind == "memory_removal",
                                               MemoryCandidate.source_chapter == 5).all()
        assert len(rem) == 1
        assert rem[0].payload["memory_type"] == "fact"


def test_correct_memory_no_content_400(temp_project):
    """无正文的章不能校正。"""
    with tenant_session(temp_project) as db:
        ch = Chapter(project_id=uuid.UUID(temp_project), chapter_seq=8, content=None,
                     status="confirmed")
        db.add(ch)
        db.commit()
        ch_id = str(ch.id)
    try:
        correct_chapter_memory(temp_project, ch_id)
        raise AssertionError("应 400")
    except HTTPException as exc:
        assert exc.status_code == 400


# ---- 编辑正文端点 ----

def test_update_chapter_content_increments_version(temp_project):
    """编辑保存：只更新正文 + 版本递增，不动记忆。"""
    ch_id = _seed_chapter(temp_project, 3, "原标题正文")
    r = update_chapter_content(temp_project, ch_id, ContentUpdate(content="改标点后的正文。", expected_version=1))
    assert r["version"] == 2
    with tenant_session(temp_project) as db:
        ch = db.get(Chapter, uuid.UUID(ch_id))
        assert ch.content == "改标点后的正文。"
        assert ch.version == 2


def _find_chapter_id(pid: str, seq: int) -> str:
    with tenant_session(pid) as db:
        return str(db.query(Chapter).filter(Chapter.chapter_seq == seq).first().id)
