"""章节修改后记忆失效与重建索引（§7.3/§11.2 阶段 3，指标「旧记忆失效正确率」）。

确定性失效服务 invalidate_chapter_memory + persist 重写自愈 + 读侧生效：
- 命中章：facts/states/relations 时间窗关闭（valid_to=seq，facts 同时 expired）、
  events/foreshadows 开放项硬删、embeddings 删除；
- 无关章 0 误伤（正确率的 precision）；幂等 no-op；
- 失效只由显式重写（rewrite=True）触发，_persist_candidates 保持纯追加——
  顺序确认候选不互相覆盖（设计决策回归）；重写续跑重放已确认候选、排除已拒绝；
- get_character_state 尊重 valid_to/valid_from，召回读侧不含已失效章记忆。
"""

from __future__ import annotations

import uuid

from myink.db import tenant_session
from myink.memory import repository as repo
from myink.memory.invalidation import invalidate_chapter_memory
from myink.memory.recall import build_context
from myink.memory.vector_store import PgvectorStore
from myink.models import CharacterState, EmbeddingRow, Event, Fact, Foreshadow, Relation
from myink.workflow import nodes

CID, CID2 = uuid.uuid4(), uuid.uuid4()
ZERO_VEC = [0.0] * 1024


# ---- 种子辅助 ----

def _seed_fact(pid: str, seq: int, content: str, *, is_hard: bool = False) -> None:
    with tenant_session(pid) as db:
        db.add(Fact(project_id=uuid.UUID(pid), content=content, category="规则" if is_hard else "关系",
                    is_hard=is_hard, source_chapter=seq, confidence=0.9,
                    confirm_status="confirmed"))
        db.commit()


def _seed_state(pid: str, seq: int, *, new_value: str = "某地", **kw) -> None:
    fields = {"character_id": CID, "chapter_seq": seq, "field": "location",
              "old_value": None, "new_value": new_value, "source_chapter": seq,
              "confidence": 0.9, "valid_to": None}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(CharacterState(project_id=uuid.UUID(pid), **fields))
        db.commit()


def _seed_relation(pid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        db.add(Relation(project_id=uuid.UUID(pid), source_id=CID, relation_type="ally",
                        target_id=CID2, source_chapter=seq, confidence=0.9))
        db.commit()


def _seed_event(pid: str, seq: int, summary: str, *, with_vector: bool = True) -> None:
    with tenant_session(pid) as db:
        ev = Event(project_id=uuid.UUID(pid), summary=summary, source_chapter=seq, confidence=0.9)
        db.add(ev)
        db.flush()
        if with_vector:
            PgvectorStore().upsert(db, project_id=uuid.UUID(pid), level="event",
                                   source_id=ev.id, source_chapter=seq,
                                   model_version="bge-m3", embedding=ZERO_VEC)
        db.commit()


def _seed_foreshadow(pid: str, seq: int, *, status: str = "planted") -> None:
    with tenant_session(pid) as db:
        db.add(Foreshadow(project_id=uuid.UUID(pid), description=f"伏笔{seq}",
                          status=status, planted_chapter=seq, trigger={}))
        db.commit()


# ---- 失效正确率：命中章全失效 + 无关章 0 误伤 ----

def test_invalidate_target_chapter_fully_invalidated(temp_project):
    """命中章 ch5 全量记忆 → facts(非硬+硬) 关闭且 expired、state/relation 关闭、
    event/开放伏笔删除、embeddings 删除；stats 与种子数对账。"""
    _seed_fact(temp_project, 5, "事实5", is_hard=False)
    _seed_fact(temp_project, 5, "硬约束5", is_hard=True)
    _seed_state(temp_project, 5)
    _seed_relation(temp_project, 5)
    _seed_event(temp_project, 5, "事件5")
    _seed_foreshadow(temp_project, 5)

    with tenant_session(temp_project) as db:
        stats = invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()

    assert stats["facts_closed"] == 2
    assert stats["states_closed"] == 1
    assert stats["relations_closed"] == 1
    assert stats["events_deleted"] == 1
    assert stats["foreshadows_closed"] == 1
    assert stats["embeddings_deleted"] >= 1  # event 向量（world 向量未在此章种，≥1 即可）

    with tenant_session(temp_project) as db:
        facts = db.query(Fact).filter(Fact.source_chapter == 5).all()
        assert all(f.valid_to == 5 and f.confirm_status == "expired" for f in facts)
        states = db.query(CharacterState).filter(CharacterState.source_chapter == 5).all()
        assert all(s.valid_to == 5 for s in states)
        rels = db.query(Relation).filter(Relation.source_chapter == 5).all()
        assert all(r.valid_to == 5 for r in rels)
        assert db.query(Event).filter(Event.source_chapter == 5).count() == 0
        assert db.query(Foreshadow).filter(Foreshadow.planted_chapter == 5).count() == 0
        emb = db.query(EmbeddingRow) \
            .filter(EmbeddingRow.source_chapter == 5).count()
        assert emb == 0


def test_invalidate_no_collateral_other_chapter(temp_project):
    """无关章 ch6 记忆不受 ch5 失效影响（0 误伤）。"""
    for seq in (5, 6):
        _seed_fact(temp_project, seq, f"事实{seq}", is_hard=False)
        _seed_event(temp_project, seq, f"事件{seq}")
    with tenant_session(temp_project) as db:
        invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()
    with tenant_session(temp_project) as db:
        facts6 = db.query(Fact).filter(Fact.source_chapter == 6).all()
        assert all(f.valid_to is None and f.confirm_status == "confirmed" for f in facts6)
        assert db.query(Event).filter(Event.source_chapter == 6).count() == 1
        emb6 = db.query(EmbeddingRow) \
            .filter(EmbeddingRow.source_chapter == 6).count()
        assert emb6 == 1


def test_invalidate_idempotent_noop(temp_project):
    """二次失效 no-op（已关闭行不被重复计数）。"""
    _seed_fact(temp_project, 5, "事实5", is_hard=False)
    _seed_event(temp_project, 5, "事件5")
    with tenant_session(temp_project) as db:
        invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()
        stats = invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()
    assert stats == {"facts_closed": 0, "states_closed": 0, "relations_closed": 0,
                     "events_deleted": 0, "foreshadows_closed": 0, "embeddings_deleted": 0}


def test_invalidate_empty_chapter_noop(temp_project):
    """空章失效全 0 不报错。"""
    with tenant_session(temp_project) as db:
        stats = invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=7)
        db.flush()
    assert stats["facts_closed"] == 0 and stats["embeddings_deleted"] == 0


def test_invalidate_hard_fact_expired_removed_from_recall(temp_project):
    """硬约束事实失效靠 confirm_status=expired 被 get_hard_facts 剔除（valid_to 单列对硬事实无效）。"""
    _seed_fact(temp_project, 5, "硬约束5", is_hard=True)
    _seed_fact(temp_project, 6, "硬约束6", is_hard=True)
    with tenant_session(temp_project) as db:
        invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()
        facts = repo.get_hard_facts(db, uuid.UUID(temp_project), chapter_seq=5)
    contents = [f.content for f in facts]
    assert "硬约束5" not in contents
    assert "硬约束6" in contents


def test_metric_compute_correctness(temp_project):
    """§16「旧记忆失效正确率」：target 全命中（missed==0）且 collateral==0 → 1.0。"""
    for seq in (5, 6):
        _seed_fact(temp_project, seq, f"事实{seq}", is_hard=False)
        _seed_state(temp_project, seq, new_value=f"地点{seq}")
        _seed_event(temp_project, seq, f"事件{seq}")
    with tenant_session(temp_project) as db:
        stats = invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()
    # target：ch5 应失效对象 = 非硬事实1 + state1 + 事件1 = 3（relation/伏笔未种）
    missed = 3 - (stats["facts_closed"] + stats["states_closed"] + stats["events_deleted"])
    # collateral：ch6 的 3 个对象都应仍在效
    with tenant_session(temp_project) as db:
        p = uuid.UUID(temp_project)
        collateral = 0
        for f in db.query(Fact).filter(Fact.source_chapter == 6).all():
            collateral += f.valid_to is not None
        for s in db.query(CharacterState).filter(CharacterState.source_chapter == 6).all():
            collateral += s.valid_to is not None
        collateral += db.query(Event).filter(Event.source_chapter == 6).count() != 1
    assert missed == 0, f"target 未全命中: {missed}"
    assert collateral == 0, f"无关章误伤: {collateral}"


# ---- persist 重写自愈：先失效再写、无重复 ----

def test_persist_rewrite_invalidates_then_writes_no_duplicate(temp_project, fake_embedder):
    """模拟重写：invalidate 后 _persist_candidates(skip_pool_handled=False) →
    旧事件/向量删除、旧事实关闭、新事件/向量唯一、无重复行。"""
    _seed_fact(temp_project, 5, "旧事实", is_hard=False)
    _seed_event(temp_project, 5, "旧事件")
    new_cands = [
        {"kind": "event", "source_chapter": 5,
         "payload": {"summary": "新事件", "participants": []}, "confidence": 0.9},
        {"kind": "fact", "source_chapter": 5,
         "payload": {"content": "新事实", "category": "关系", "is_hard": False}, "confidence": 0.9},
    ]
    with tenant_session(temp_project) as db:
        invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        nodes._persist_candidates(db, temp_project, 5, new_cands, skip_pool_handled=False)
        db.flush()
    with tenant_session(temp_project) as db:
        events = db.query(Event).filter(Event.source_chapter == 5).all()
        assert len(events) == 1 and events[0].summary == "新事件"  # 旧事件已删，无重复
        # 事实用时间窗关闭（保留历史供证据链）：旧行关闭、新行活跃
        facts = db.query(Fact).filter(Fact.source_chapter == 5).all()
        assert len(facts) == 2
        active = [f for f in facts if f.valid_to is None]
        closed = [f for f in facts if f.valid_to is not None]
        assert len(active) == 1 and active[0].content == "新事实"
        assert len(closed) == 1 and closed[0].content == "旧事实" and closed[0].valid_to == 5
        emb = db.query(EmbeddingRow) \
            .filter(EmbeddingRow.source_chapter == 5).count()
        assert emb == 2  # 新事件 + 新事实各一条向量，无旧残留


def test_confirm_candidate_sequence_does_not_lose_prior(temp_project, fake_embedder):
    """设计决策回归：失效不放 _persist_candidates 顶部 → 顺序确认两条候选两条都在
    （若顶部无条件失效，确认 C2 会失效 C1 刚落库的记忆）。"""
    c1 = {"kind": "event", "source_chapter": 5,
          "payload": {"summary": "C1", "participants": []}, "confidence": 0.9}
    c2 = {"kind": "event", "source_chapter": 5,
          "payload": {"summary": "C2", "participants": []}, "confidence": 0.9}
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 5, [c1], skip_pool_handled=False)
        db.flush()
        nodes._persist_candidates(db, temp_project, 5, [c2], skip_pool_handled=False)
        db.flush()
    with tenant_session(temp_project) as db:
        summaries = {e.summary for e in db.query(Event).filter(Event.source_chapter == 5).all()}
    assert summaries == {"C1", "C2"}


def test_reapply_pool_for_rewrite_preserves_confirmed_excludes_rejected(temp_project):
    """重写续跑池重放：补已确认候选、排除已拒绝候选、pending 不动。"""
    from myink.models import MemoryCandidate

    def _pool(kind: str, payload: dict, status: str) -> None:
        with tenant_session(temp_project) as db:
            db.add(MemoryCandidate(project_id=uuid.UUID(temp_project), kind=kind,
                                   source_chapter=5, payload=payload, confidence=0.9, status=status))
            db.commit()

    _pool("event", {"summary": "X"}, "confirmed")
    _pool("event", {"summary": "Y"}, "rejected")
    state_cands = [
        {"kind": "event", "source_chapter": 5, "payload": {"summary": "Y"},
         "confidence": 0.9},   # state 再产出被拒项 → 排除
        {"kind": "event", "source_chapter": 5, "payload": {"summary": "Z"},
         "confidence": 0.9},   # 新产出
    ]
    with tenant_session(temp_project) as db:
        out = nodes._reapply_pool_for_rewrite(db, temp_project, 5, state_cands)
    got = [c["payload"]["summary"] for c in out]
    assert "Y" not in got          # rejected 不复活
    assert "Z" in got              # state 新产出保留
    assert "X" in got              # confirmed 补入（防确认记忆丢失）


# ---- 读侧生效 ----

def test_get_character_state_ignores_invalidated(temp_project):
    """旧状态失效（valid_to=seq）+ 重写后新状态同 field → 返回新值（真实顺序：先失效后写）。"""
    _seed_state(temp_project, 5, new_value="旧值")
    with tenant_session(temp_project) as db:
        invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()
        # 重写后 persist 写新状态（在失效之后 → 不被关闭）
        db.add(CharacterState(project_id=uuid.UUID(temp_project), character_id=CID,
                              chapter_seq=5, field="location", old_value="旧值",
                              new_value="新值", source_chapter=5, confidence=0.9))
        db.flush()
        st = repo.get_character_state(db, uuid.UUID(temp_project), CID, chapter_seq=5)
    assert st.get("location") == "新值"  # 旧行已关闭，只剩新行


def test_get_character_state_valid_from_boundary(temp_project):
    """valid_from > 查询章 的状态不计入（时序快照 §7.7）。"""
    _seed_state(temp_project, 5, new_value="未来态", valid_from=10)
    with tenant_session(temp_project) as db:
        st = repo.get_character_state(db, uuid.UUID(temp_project), CID, chapter_seq=5)
    assert "location" not in st


def test_recall_after_invalidate_no_stale_keeps_other(temp_project, fake_embedder):
    """§16 读侧闭环：写 ch7 时不含已失效 ch5 事实/事件、仍含历史 ch6。"""
    _seed_fact(temp_project, 5, "事实5", is_hard=False)
    _seed_fact(temp_project, 6, "事实6", is_hard=False)
    _seed_event(temp_project, 5, "事件5")
    _seed_event(temp_project, 6, "事件6")
    with tenant_session(temp_project) as db:
        invalidate_chapter_memory(db, project_id=uuid.UUID(temp_project), chapter_seq=5)
        db.flush()
        ctx = build_context(db, project_id=uuid.UUID(temp_project), chapter_seq=7)
    fact_contents = [f["content"] for f in ctx.long_term_facts]
    event_summaries = [e["summary"] for e in ctx.mid_term_events]
    assert "事实5" not in fact_contents and "事实6" in fact_contents
    assert "事件5" not in event_summaries and "事件6" in event_summaries


# ---- VectorStore.delete ----

def test_vector_store_delete_by_chapter(temp_project):
    """按 source_chapter 删除向量，rowcount 正确、只删该章。"""
    from myink.models import EmbeddingRow

    with tenant_session(temp_project) as db:
        for seq, s_id in ((5, uuid.uuid4()), (6, uuid.uuid4())):
            PgvectorStore().upsert(db, project_id=uuid.UUID(temp_project), level="event",
                                   source_id=s_id, source_chapter=seq,
                                   model_version="bge-m3", embedding=ZERO_VEC)
        db.flush()
        n = PgvectorStore().delete(db, project_id=uuid.UUID(temp_project), source_chapter=5)
        db.flush()
        remaining = db.query(EmbeddingRow).filter(EmbeddingRow.project_id == uuid.UUID(temp_project)).count()
    assert n == 1
    assert remaining == 1
