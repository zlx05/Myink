"""级联删除章节（阶段 3：删该章及其后全部正文 + 记忆 + 池候选 + 登记表产物，进度回退）。

决策（阶段 3 对齐）：中间章删掉后后续章剧情引用已删事件会断层 → 级联删除让作者
从被删章重新生成；Project.current_chapter 回退到保留的最大章序。复用失效原语
invalidate_chapter_memory（facts/states/relations 关窗、events/foreshadows/embeddings 删）
+ purge_deleted_chapter_registry（被删章首次出现的设定实体、只在被删章出现的人物 + 别名
清理；建书确认的势力/地点/规则保留）。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from myink.api.routes_chapters import delete_chapter
from myink.db import tenant_session
from myink.memory.vector_store import PgvectorStore
from myink.models import (Alias, Chapter, Character, CharacterState, EmbeddingRow,
                          Entity, Event, MemoryCandidate, Project, Relation)

ZERO_VEC = [0.0] * 1024


def _seed_chapter(pid: str, seq: int) -> str:
    with tenant_session(pid) as db:
        ch = Chapter(project_id=uuid.UUID(pid), chapter_seq=seq, title=f"第{seq}章",
                     content=f"第{seq}章正文", status="confirmed", generation_source="auto")
        db.add(ch)
        db.commit()
        return str(ch.id)


def _seed_event_with_vec(pid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        ev = Event(project_id=uuid.UUID(pid), summary=f"事件{seq}", source_chapter=seq, confidence=0.9)
        db.add(ev)
        db.flush()
        PgvectorStore().upsert(db, project_id=uuid.UUID(pid), level="event", source_id=ev.id,
                               source_chapter=seq, model_version="bge-m3", embedding=ZERO_VEC)
        db.commit()


def _seed_pool_candidate(pid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        db.add(MemoryCandidate(project_id=uuid.UUID(pid), kind="event", source_chapter=seq,
                               payload={"summary": f"候选{seq}", "participants": []}, confidence=0.8))
        db.commit()


def _set_current_chapter(pid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        proj = db.query(Project).filter(Project.id == uuid.UUID(pid)).first()
        proj.current_chapter = seq
        db.commit()


def _seed_entity(pid: str, name: str, first_seen: int, etype: str = "item") -> str:
    """自动建档设定实体 + 别名（对齐 nodes.py:951-956 persist 口径）。"""
    with tenant_session(pid) as db:
        ent = Entity(project_id=uuid.UUID(pid), entity_type=etype, canonical_name=name,
                     properties={"first_seen_chapter": first_seen})
        db.add(ent)
        db.flush()
        db.add(Alias(project_id=uuid.UUID(pid), alias=name, entity_id=ent.id))
        db.commit()
        return str(ent.id)


def _seed_character(pid: str, name: str) -> str:
    """自动建档人物卡 + 别名（对齐 nodes.py:935-941 persist 口径）。"""
    with tenant_session(pid) as db:
        ch = Character(project_id=uuid.UUID(pid), name=name, realm_cap="无")
        db.add(ch)
        db.flush()
        db.add(Alias(project_id=uuid.UUID(pid), alias=name, entity_id=ch.id))
        db.commit()
        return str(ch.id)


def _seed_character_state(pid: str, cid: str, seq: int, field: str = "realm") -> None:
    with tenant_session(pid) as db:
        db.add(CharacterState(project_id=uuid.UUID(pid), character_id=uuid.UUID(cid),
                              chapter_seq=seq, field=field, new_value=f"v{seq}",
                              source_chapter=seq, valid_from=seq))
        db.commit()


def _seed_event_participant(pid: str, cid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        db.add(Event(project_id=uuid.UUID(pid), summary=f"事件{seq}",
                     participants=[str(cid)], source_chapter=seq, confidence=0.9))
        db.commit()


def _seed_relation(pid: str, sid: str, tid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        db.add(Relation(project_id=uuid.UUID(pid), source_id=uuid.UUID(sid),
                        relation_type="knows", target_id=uuid.UUID(tid),
                        source_chapter=seq, valid_from=seq))
        db.commit()


def test_delete_chapter_cascades_tail_and_memories(temp_project):
    """删 ch4 → ch4/5/6 正文、事件+向量、池候选全删；ch2/3 保留；进度回退到 3。"""
    ids = {seq: _seed_chapter(temp_project, seq) for seq in (2, 3, 4, 5, 6)}
    for seq in (4, 5, 6):
        _seed_event_with_vec(temp_project, seq)
        _seed_pool_candidate(temp_project, seq)
    _set_current_chapter(temp_project, 6)

    result = delete_chapter(temp_project, ids[4])
    assert [d["chapter_seq"] for d in result["deleted"]] == [4, 5, 6]
    assert result["current_chapter"] == 3

    with tenant_session(temp_project) as db:
        remain = [c.chapter_seq for c in db.query(Chapter).order_by(Chapter.chapter_seq).all()]
        assert remain == [2, 3]
        assert db.query(Event).filter(Event.source_chapter.in_([4, 5, 6])).count() == 0
        assert db.query(EmbeddingRow).filter(EmbeddingRow.source_chapter.in_([4, 5, 6])).count() == 0
        assert db.query(MemoryCandidate).filter(MemoryCandidate.source_chapter.in_([4, 5, 6])).count() == 0
        assert db.query(Project).filter(Project.id == uuid.UUID(temp_project)).first().current_chapter == 3


def test_delete_tail_rolls_back_current_chapter(temp_project):
    """删尾部章（无后续）→ 进度回退到保留最大章。"""
    _seed_chapter(temp_project, 2)
    id6 = _seed_chapter(temp_project, 6)
    _seed_chapter(temp_project, 7)
    _set_current_chapter(temp_project, 7)

    result = delete_chapter(temp_project, id6)
    assert [d["chapter_seq"] for d in result["deleted"]] == [6, 7]
    assert result["current_chapter"] == 2


def test_delete_all_chapters_resets_progress(temp_project):
    """删光全部章 → 进度回 0。"""
    id1 = _seed_chapter(temp_project, 1)
    _set_current_chapter(temp_project, 1)
    result = delete_chapter(temp_project, id1)
    assert result["current_chapter"] == 0
    with tenant_session(temp_project) as db:
        assert db.query(Chapter).count() == 0


def test_delete_chapter_missing_404(temp_project):
    try:
        delete_chapter(temp_project, str(uuid.uuid4()))
        raise AssertionError("应 404")
    except HTTPException as exc:
        assert exc.status_code == 404


def test_delete_keeps_earlier_chapter_memories(temp_project):
    """级联只清 [seq, tail]，被删章之前的记忆不受影响（0 误伤）。"""
    _seed_chapter(temp_project, 2)
    _seed_event_with_vec(temp_project, 2)
    id3 = _seed_chapter(temp_project, 3)
    _seed_event_with_vec(temp_project, 3)

    delete_chapter(temp_project, id3)
    with tenant_session(temp_project) as db:
        assert db.query(Event).filter(Event.source_chapter == 2).count() == 1
        assert db.query(EmbeddingRow).filter(EmbeddingRow.source_chapter == 2).count() == 1


# ---- 登记表产物清理（purge_deleted_chapter_registry，§审计整改：删章不再残留人物卡/武器）----


def test_delete_purges_entities_introduced_in_deleted_tail(temp_project):
    """删 ch4 → 首次出现在 ch4/5 的设定实体（武器）+ 别名删除；保留章实体（ch2）留存。"""
    _seed_chapter(temp_project, 2)
    _seed_chapter(temp_project, 3)
    id4 = _seed_chapter(temp_project, 4)
    _seed_entity(temp_project, "玄铁剑", 2)
    _seed_entity(temp_project, "断魂鞭", 4)
    _seed_entity(temp_project, "摄魂灯", 5)

    delete_chapter(temp_project, id4)
    with tenant_session(temp_project) as db:
        names = [e.canonical_name for e in db.query(Entity).all()]
        assert names == ["玄铁剑"], "被删章首次出现的武器应清除，保留章武器留存"
        assert db.query(Alias).count() == 1, "已删实体的别名应一并清除"


def test_delete_purges_characters_only_in_deleted_tail(temp_project):
    """删 ch4/5 → 只在被删章出现的人物（state / 事件参与者）删除；保留章出现/无引用的人物留。"""
    _seed_chapter(temp_project, 2)
    _seed_chapter(temp_project, 3)
    id4 = _seed_chapter(temp_project, 4)
    _seed_chapter(temp_project, 5)
    a = _seed_character(temp_project, "秦无霜")
    _seed_character_state(temp_project, a, 5)  # 只在被删章（state）→ 删
    b = _seed_character(temp_project, "林尘")
    _seed_character_state(temp_project, b, 2)  # 保留章 + 被删章都出现 → 留
    _seed_character_state(temp_project, b, 5)
    _seed_character(temp_project, "掌门")  # 纯设定、无任何章引用 → 留
    d = _seed_character(temp_project, "赵铁柱")
    _seed_event_participant(temp_project, d, 5)  # 只以事件参与者出现在被删章 → 删

    delete_chapter(temp_project, id4)
    with tenant_session(temp_project) as db:
        names = sorted(c.name for c in db.query(Character).all())
        assert names == ["掌门", "林尘"]  # sorted() 按 Unicode 码点，掌门(U+638C) 在 林尘(U+6797) 前
        assert db.query(Alias).filter(Alias.entity_id.in_([uuid.UUID(a), uuid.UUID(d)])).count() == 0


def test_delete_purge_cleans_dangling_relation_and_keeps_earlier_registry(temp_project):
    """删 ch3 → 只在 ch3 出现的人物删除 + 指向它的悬空关系一并删；保留章人物/实体/状态留存。"""
    _seed_chapter(temp_project, 2)
    id3 = _seed_chapter(temp_project, 3)
    _seed_entity(temp_project, "玄铁剑", 2)
    c = _seed_character(temp_project, "林尘")
    _seed_character_state(temp_project, c, 2)
    c2 = _seed_character(temp_project, "秦无霜")
    _seed_character_state(temp_project, c2, 3)
    _seed_relation(temp_project, c, c2, 3)

    delete_chapter(temp_project, id3)
    with tenant_session(temp_project) as db:
        names = sorted(x.name for x in db.query(Character).all())
        assert names == ["林尘"]
        assert db.query(Relation).count() == 0, "指向已删人物的关系应删除，不留悬空边"
        assert db.query(Entity).filter(Entity.canonical_name == "玄铁剑").count() == 1
        assert db.query(CharacterState).filter(CharacterState.character_id == uuid.UUID(c)).count() == 1
