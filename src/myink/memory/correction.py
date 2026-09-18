"""正文编辑后的记忆增量校正（阶段 3 切片，plan.md §7.3 扩展）。

用户对已落库章节做轻编辑（语言风格 / 句子长短 / 标点）后默认不动记忆——纯风格
编辑重新抽取的记忆与已落库记忆逐条命中 → keep 全量、池零新增（「不要太大动作」）；
真正删改剧情内容时才产出变更集（新增 / 删除），进待确认池人工确认后生效（数据流
边界 §6.2：池落库须 confirm，Agent 不直写）。

删除项以 memory_removal 候选表达（payload={memory_type, memory_id, display}），
confirm 时按类型复用失效语义（facts/states/relations 时间窗关闭、fact 同时 expired、
events/foreshadows 硬删 + 关联向量删除，与 invalidate_chapter_memory 同口径，§7.3）。

匹配口径：结构化记忆（character_state / relation）按稳定键精确匹配；自由文本
（event 摘要 / fact 内容 / foreshadow 描述）归一化后宽松匹配（子串或字符级相似度
≥ 阈值）——容忍 LLM 对同义句的措辞抖动，纯风格编辑不触发误删误增。
"""

from __future__ import annotations

import difflib
import re
import uuid

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.orm import Session

from myink.models import CharacterState, EmbeddingRow, Event, Fact, Foreshadow, MemoryCandidate, Relation

# 自由文本宽松匹配阈值（字符级 SequenceMatcher 比率，0.85 容忍措辞抖动）
_FUZZY_THRESHOLD = 0.85

_PUNCT = re.compile(r"[]\s，。！？、；：""''（）《》【】…—～·~,.;:!?()[-]+")


def _sig(text: str) -> str:
    """归一化签名：去空白与标点，供自由文本宽松比较。"""
    return _PUNCT.sub("", text or "")


def _close(a: str, b: str) -> bool:
    """宽松相等：归一化后全等 / 子串 / 字符级相似度 ≥ 阈值。"""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _FUZZY_THRESHOLD


# ---- 已落库记忆装载（可比对结构）----


def load_persisted_memories(db: Session, *, project_id: uuid.UUID,
                            chapter_seq: int) -> list[dict]:
    """该章已落库记忆（活性行）→ 可比对结构 [{memory_type, memory_id, kind, display, key/text}]。

    - events：一次性记忆无时间窗，整章全取；
    - facts / character_states / relations：仅活性行（confirmed + valid_to IS NULL）；
    - foreshadows：该章种植且仍开放（planted/developing），resolved/dropped 保留历史不参与。
    """
    memories: list[dict] = []

    for ev in db.query(Event).filter(Event.project_id == project_id,
                                     Event.source_chapter == chapter_seq).all():
        memories.append({
            "memory_type": "event", "memory_id": str(ev.id), "kind": "event",
            "display": ev.summary, "text": _sig(ev.summary),
        })

    for f in db.query(Fact).filter(Fact.project_id == project_id,
                                   Fact.source_chapter == chapter_seq,
                                   Fact.confirm_status == "confirmed",
                                   Fact.valid_to.is_(None)).all():
        memories.append({
            "memory_type": "fact", "memory_id": str(f.id), "kind": "fact",
            "display": f.content, "text": _sig(f.content),
        })

    for s in db.query(CharacterState).filter(CharacterState.project_id == project_id,
                                             CharacterState.source_chapter == chapter_seq,
                                             CharacterState.valid_to.is_(None)).all():
        memories.append({
            "memory_type": "character_state", "memory_id": str(s.id),
            "kind": "character_state", "display": f"{s.field}: {s.new_value}",
            "text": _sig(s.old_value or "") + _sig(s.new_value or ""),
            "key": ("character_state", str(s.character_id), s.field, s.old_value, s.new_value),
        })

    for r in db.query(Relation).filter(Relation.project_id == project_id,
                                       Relation.source_chapter == chapter_seq,
                                       Relation.valid_to.is_(None)).all():
        memories.append({
            "memory_type": "relation", "memory_id": str(r.id), "kind": "relation_change",
            "display": f"{r.relation_type}",
            "key": ("relation", str(r.source_id), r.relation_type, str(r.target_id)),
        })

    for fo in db.query(Foreshadow).filter(Foreshadow.project_id == project_id,
                                          Foreshadow.planted_chapter == chapter_seq,
                                          Foreshadow.status.in_(["planted", "developing"])).all():
        memories.append({
            "memory_type": "foreshadow", "memory_id": str(fo.id), "kind": "foreshadow",
            "display": fo.description, "text": _sig(fo.description),
        })
    return memories


# ---- 匹配与变更集 ----


def _matches(cand_kind: str, payload: dict, mem: dict) -> bool:
    """新候选是否命中某条已落库记忆（结构化精确 / 自由文本宽松）。"""
    p = payload or {}
    if mem["kind"] == "character_state":
        return cand_kind == "character_state" and mem["key"] == (
            "character_state", str(p.get("character_id")), p.get("field"),
            p.get("old_value"), p.get("new_value"))
    if mem["kind"] == "relation_change":
        return cand_kind == "relation_change" and mem["key"] == (
            "relation", str(p.get("source_id")), p.get("relation_type"),
            str(p.get("target_id")))
    if mem["kind"] == "event":
        return cand_kind == "event" and _close(_sig(p.get("summary", "")), mem["text"])
    if mem["kind"] == "fact":
        return cand_kind == "fact" and _close(_sig(p.get("content", "")), mem["text"])
    if mem["kind"] == "foreshadow":
        return cand_kind == "foreshadow" and _close(_sig(p.get("description", "")), mem["text"])
    return False


def diff_changeset(persisted: list[dict], new_candidates: list[dict]) -> dict:
    """编辑后新候选 vs 该章已落库记忆 → 变更集 {add, remove, keep}。

    贪心匹配：每条候选至多命中一条未匹配记忆（命中即 keep、该记忆移出待删集），
    防同义候选重复匹配多条（摘要被换措辞后与多条旧摘要都相似时只占一条）。
    """
    unmatched = list(persisted)
    added: list[dict] = []
    keep = 0
    for cand in new_candidates:
        payload = cand.get("payload") or {}
        hit = next((m for m in unmatched if _matches(cand["kind"], payload, m)), None)
        if hit is not None:
            unmatched.remove(hit)
            keep += 1
        else:
            added.append(cand)
    return {"add": added, "remove": unmatched, "keep": keep}


# ---- 池写入（幂等）----


def _pool_has_duplicate(db: Session, project_id: uuid.UUID, chapter_seq: int,
                        cand: dict) -> bool:
    """池内去重：同 kind+payload+章 的候选已存在（任意状态）则跳过（口径同 nodes）。"""
    rows = db.execute(
        select(MemoryCandidate).where(
            MemoryCandidate.project_id == project_id,
            MemoryCandidate.kind == cand["kind"],
            MemoryCandidate.source_chapter == chapter_seq,
        )
    ).scalars().all()
    return any(r.payload == cand["payload"] for r in rows)


def apply_changeset_to_pool(db: Session, *, project_id: uuid.UUID, chapter_seq: int,
                            changeset: dict) -> dict:
    """变更集 → 待确认池（数据流边界 §6.2：人工确认后落库/失效）。

    add 候选原样入池（kind 为 extract 正常 kind）；remove 项以 memory_removal 候选入池
    （payload={memory_type, memory_id, display}），confirm 时按类型失效该记忆。
    幂等：池内已有同 (kind, payload) 该章候选则跳过。
    """
    added = removed = dup = 0
    for cand in changeset["add"]:
        if _pool_has_duplicate(db, project_id, chapter_seq, cand):
            dup += 1
            continue
        db.add(MemoryCandidate(project_id=project_id, kind=cand["kind"],
                               source_chapter=chapter_seq, payload=cand.get("payload") or {},
                               confidence=cand.get("confidence", 0.0)))
        added += 1
    for mem in changeset["remove"]:
        payload = {"memory_type": mem["memory_type"], "memory_id": mem["memory_id"],
                   "display": mem.get("display")}
        if _pool_has_duplicate(db, project_id, chapter_seq,
                               {"kind": "memory_removal", "payload": payload}):
            dup += 1
            continue
        db.add(MemoryCandidate(project_id=project_id, kind="memory_removal",
                               source_chapter=chapter_seq, payload=payload, confidence=1.0))
        removed += 1
    return {"add_to_pool": added, "remove_to_pool": removed, "duplicates_skipped": dup}


# ---- 删除候选确认（confirm 时按类型失效）----


def apply_memory_removal(db: Session, *, project_id: uuid.UUID, payload: dict,
                         chapter_seq: int) -> bool:
    """确认 memory_removal 候选：按类型失效被删记忆（复用失效语义，§7.3）。

    event/foreshadow 硬删 + event 关联向量删除；fact/character_state/relation 时间窗关闭
    （fact 同时 expired，硬约束靠 confirm_status 被 get_hard_facts 剔除）。目标记忆已不
    存在视为已删除 → True（幂等，重写后残留删除候选可确认）。RLS 按租户隔离，跨租户
    db.get 返回 None，不误删。
    """
    mtype = payload.get("memory_type") if isinstance(payload, dict) else None
    mid_raw = payload.get("memory_id") if isinstance(payload, dict) else None
    if not mtype or not mid_raw:
        return False
    try:
        mid = uuid.UUID(str(mid_raw))
    except (ValueError, TypeError):
        return False

    if mtype == "event":
        ev = db.get(Event, mid)
        if ev is None:
            return True  # 已删 → 幂等成功
        db.execute(sa_delete(EmbeddingRow).where(EmbeddingRow.project_id == project_id,
                                                 EmbeddingRow.source_id == mid))
        db.delete(ev)
        return True
    if mtype == "fact":
        f = db.get(Fact, mid)
        if f is None:
            return True
        f.valid_to = chapter_seq
        f.confirm_status = "expired"
        return True
    if mtype == "character_state":
        s = db.get(CharacterState, mid)
        if s is None:
            return True
        s.valid_to = chapter_seq
        return True
    if mtype == "relation":
        r = db.get(Relation, mid)
        if r is None:
            return True
        r.valid_to = chapter_seq
        return True
    if mtype == "foreshadow":
        fo = db.get(Foreshadow, mid)
        if fo is None:
            return True
        db.delete(fo)
        return True
    return False


# ---- 编排入口 ----


def correct_chapter_memory(db: Session, *, project_id: uuid.UUID, chapter_seq: int,
                           new_candidates: list[dict]) -> dict:
    """编辑后记忆校正：diff → 变更集 → 待确认池，返回变更报告（阶段 3）。

    纯风格编辑（改语言/标点/句长）时新候选与已落库记忆逐条命中 → keep 全量、池零新增，
    no_op=True 实现「轻编辑默认零动作」；删改剧情内容时才产出 add/remove 候选待人工确认。
    """
    persisted = load_persisted_memories(db, project_id=project_id, chapter_seq=chapter_seq)
    changeset = diff_changeset(persisted, new_candidates)
    pool_stats = apply_changeset_to_pool(db, project_id=project_id,
                                         chapter_seq=chapter_seq, changeset=changeset)
    return {
        "chapter_seq": chapter_seq,
        "changeset": {"add": len(changeset["add"]), "remove": len(changeset["remove"]),
                      "keep": changeset["keep"]},
        "pool": pool_stats,
        "no_op": not changeset["add"] and not changeset["remove"],
    }
