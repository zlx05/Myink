"""章节修改后记忆失效与重建索引（§7.3/§11.2，阶段 3 落地）。

确定性失效服务，编排层调用（数据流边界 §6.2：Agent 不直写）。必须在 tenant_session
内调用（RLS 按 project_id 隔离，§14.1）。语义：

- facts / character_states / relations：时间窗关闭（valid_to = chapter_seq）——长期记忆
  用时间窗表达「新事实覆盖旧事实」（§7.3），保留行供证据链追溯；
- facts 同时置 confirm_status="expired"——get_hard_facts 按 confirm_status=="confirmed"
  过滤，单独 valid_to 对硬约束事实无效（§7.2 硬约束恒在 Top-K），expired 双保险剔除；
- events / foreshadows（planted/developing）：硬删除——一次性记忆随重写作废（重写后旧
  事件 = 从未发生）；Event 无 valid_to 列，不引入 schema 变更（无迁移设施）；
- embeddings：按 source_chapter 删除（PgvectorStore.delete），新记忆 persist 时重写。

幂等：无旧记忆时各表 0 行受影响，返回全 0，不抛错。整函数确定性，不耗 LLM。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete as sa_delete, or_, select, update as sa_update
from sqlalchemy.orm import Session

from myink.memory.vector_store import PgvectorStore
from myink.models import (Alias, Character, CharacterState, Entity, Event, Fact,
                          Foreshadow, Relation)

logger = logging.getLogger(__name__)


def invalidate_chapter_memory(db: Session, *, project_id: uuid.UUID, chapter_seq: int) -> dict:
    """失效某章全部旧记忆（重写前调用），返回各表关闭/删除统计。

    与后续 _persist_candidates 在同一事务内：新写失败回滚则旧记忆不失效（原子）。
    """
    # 1. 长期事实：时间窗关闭 + expired（硬约束靠 expired 被 get_hard_facts 剔除）
    facts_closed = db.execute(
        sa_update(Fact)
        .where(Fact.project_id == project_id, Fact.source_chapter == chapter_seq,
               Fact.valid_to.is_(None))
        .values(valid_to=chapter_seq, confirm_status="expired")
    ).rowcount or 0

    # 2. 人物状态台账：时间窗关闭
    states_closed = db.execute(
        sa_update(CharacterState)
        .where(CharacterState.project_id == project_id,
               CharacterState.source_chapter == chapter_seq,
               CharacterState.valid_to.is_(None))
        .values(valid_to=chapter_seq)
    ).rowcount or 0

    # 3. 实体关系：时间窗关闭（§7.8 当前关系 = 最新一条 valid_to IS NULL）
    relations_closed = db.execute(
        sa_update(Relation)
        .where(Relation.project_id == project_id, Relation.source_chapter == chapter_seq,
               Relation.valid_to.is_(None))
        .values(valid_to=chapter_seq)
    ).rowcount or 0

    # 4. 一次性事件：硬删（重写后旧事件 = 从未发生）
    events_deleted = db.execute(
        sa_delete(Event).where(Event.project_id == project_id,
                               Event.source_chapter == chapter_seq)
    ).rowcount or 0

    # 5. 开放伏笔：硬删（resolved/dropped 为历史保留，不删）
    foreshadows_closed = db.execute(
        sa_delete(Foreshadow)
        .where(Foreshadow.project_id == project_id,
               Foreshadow.planted_chapter == chapter_seq,
               Foreshadow.status.in_(["planted", "developing"]))
    ).rowcount or 0

    # 6. 向量：按来源章删除（新记忆 persist 时重写）
    embeddings_deleted = PgvectorStore().delete(db, project_id=project_id,
                                                source_chapter=chapter_seq)

    if any((facts_closed, states_closed, relations_closed, events_deleted,
            foreshadows_closed, embeddings_deleted)):
        logger.info("失效章节记忆 ch%s（project=%s）：facts=%s states=%s relations=%s "
                    "events=%s foreshadows=%s embeddings=%s",
                    chapter_seq, project_id, facts_closed, states_closed, relations_closed,
                    events_deleted, foreshadows_closed, embeddings_deleted)
    return {
        "facts_closed": facts_closed,
        "states_closed": states_closed,
        "relations_closed": relations_closed,
        "events_deleted": events_deleted,
        "foreshadows_closed": foreshadows_closed,
        "embeddings_deleted": embeddings_deleted,
    }


def purge_deleted_chapter_registry(db: Session, *, project_id: uuid.UUID, from_seq: int) -> dict:
    """清掉被删章节产出的登记表行（章节级联删除专用；重写/编辑不调用，不误伤用户基底）。

    登记表（characters/entities/aliases）是「存在即登记」追加式注册表：整书删除走 FK
    级联，章节删除此前只失效时间窗记忆 → 删光章后人物卡/武器实体仍残留（前端设定页直读
    这些表）。本函数补齐章节删除口径：

    - entities：first_seen_chapter >= from_seq（被删章首次命名的武器/功法/技能/地点）
      → 硬删 + 相关 aliases；
    - characters：只出现在被删章节的人物（状态台账/关系/事件参与者三路引用合并，
      **最早出现章** >= from_seq）→ 硬删 + 相关 aliases + 其残留 state/relation 一并删，
      防图谱出现指向不存在节点的悬空边；建书确认但未在任何章出现的人物无引用 → 保留
      （无法判定为章节产物）；
    - 建书确认的势力/地点/世界观规则是用户基底，不在此清（用户拍板）。

    须在失效（invalidate_chapter_memory）之前调用——失效会硬删被删范围事件，而人物引用
    判定依赖事件参与者，先算才能看见；与删除同事务提交（失败回滚不落）。
    幂等：无匹配 → 全 0。整函数确定性，不耗 LLM。
    """
    entity_ids = _entities_first_seen_after(db, project_id, from_seq)
    char_ids = _characters_only_in_deleted_range(db, project_id, from_seq)
    purged = entity_ids + char_ids
    if purged:
        db.execute(sa_delete(Alias).where(Alias.project_id == project_id,
                                          Alias.entity_id.in_(purged)))
        # 被删节点的关系/台账行一并删（节点已消失，保留只会是悬空数据）
        db.execute(sa_delete(Relation).where(
            Relation.project_id == project_id,
            or_(Relation.source_id.in_(purged), Relation.target_id.in_(purged))))
        db.execute(sa_delete(CharacterState).where(
            CharacterState.project_id == project_id,
            CharacterState.character_id.in_(purged)))
    entities_deleted = 0
    if entity_ids:
        entities_deleted = db.execute(
            sa_delete(Entity).where(Entity.id.in_(entity_ids))).rowcount or 0
    characters_deleted = 0
    if char_ids:
        characters_deleted = db.execute(
            sa_delete(Character).where(Character.id.in_(char_ids))).rowcount or 0
    if entities_deleted or characters_deleted:
        logger.info("清理被删章节登记表 ch>=%s（project=%s）：entities=%s characters=%s",
                    from_seq, project_id, entities_deleted, characters_deleted)
    return {"entities_deleted": entities_deleted, "characters_deleted": characters_deleted}


def _entities_first_seen_after(db: Session, project_id: uuid.UUID,
                               from_seq: int) -> list[uuid.UUID]:
    """被删章首次出现的设定实体 id（first_seen_chapter >= from_seq；缺该字段的行保留）。

    与 entity_cards 端点同口径读 properties（JSONB 里 Python 过滤，MVP 量级够用）。
    """
    out: list[uuid.UUID] = []
    for e in db.execute(select(Entity).where(Entity.project_id == project_id)).scalars():
        first_seen = (e.properties or {}).get("first_seen_chapter")
        if first_seen is None:
            continue
        try:
            if int(first_seen) >= from_seq:
                out.append(e.id)
        except (TypeError, ValueError):
            continue
    return out


def _characters_only_in_deleted_range(db: Session, project_id: uuid.UUID,
                                      from_seq: int) -> list[uuid.UUID]:
    """只在被删章节出现的人物 id：状态台账/关系/事件参与者三路引用取**最早出现章**，
    最早章 >= from_seq 才删（在保留章出现过的 min < from_seq → 保留）。

    事件参与者存 canonical id 字符串（nodes.py:894），先转 UUID 再进集合；
    解析失败跳过（§6.12 坏数据拒绝但不崩）。
    """
    min_seen: dict[uuid.UUID, int] = {}

    def _note(cid: uuid.UUID, sc: int) -> None:
        min_seen[cid] = sc if cid not in min_seen else min(min_seen[cid], sc)

    for sc, cid in db.execute(select(CharacterState.source_chapter, CharacterState.character_id)
                              .where(CharacterState.project_id == project_id)):
        _note(cid, sc)
    for sc, sid, tid in db.execute(select(Relation.source_chapter, Relation.source_id,
                                          Relation.target_id)
                                   .where(Relation.project_id == project_id)):
        _note(sid, sc)
        _note(tid, sc)
    for sc, parts in db.execute(select(Event.source_chapter, Event.participants)
                                .where(Event.project_id == project_id)):
        for p in parts or []:
            try:
                _note(uuid.UUID(str(p)), sc)
            except (ValueError, TypeError):
                continue
    return [c.id for c in db.execute(select(Character).where(Character.project_id == project_id))
            .scalars() if c.id in min_seen and min_seen[c.id] >= from_seq]
