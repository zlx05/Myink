"""记忆读写辅助（阶段 1 最小集）。

数据流边界（§6.2）：Agent 不直接写库——写经 extract 出候选，persist（编排层）
确认或自动放行后落库。本模块只被确定性节点（recall / persist / load_state）使用。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aiink.models import (
    Alias,
    Chapter,
    ChapterOutline,
    ChapterVersion,
    Character,
    CharacterState,
    Entity,
    Event,
    Fact,
    Foreshadow,
    PlotThread,
    Project,
    ProjectSettings,
    Relation,
    VolumeOutline,
    WritingLesson,
)


# ---- 读 ----

def get_project(session: Session, project_id: uuid.UUID) -> Project | None:
    return session.get(Project, project_id)


def get_settings(session: Session, project_id: uuid.UUID) -> ProjectSettings | None:
    return session.execute(
        select(ProjectSettings).where(ProjectSettings.project_id == project_id)
    ).scalar_one_or_none()


def get_character(session: Session, project_id: uuid.UUID, name: str) -> Character | None:
    """按 canonical 名精确定位人物（§7.5 归一化；名字匹配优先）。

    name 未命中 canonical → 回退别名解析（正文以别名出现时归一到同一人物，防跨别名
    重复建卡/参与者断链）。别名行是 polymorphic（entity_id 可能是实体或人物），校验
    entity_id 确实是本项目的人物才返回。
    """
    ch = session.execute(
        select(Character).where(Character.project_id == project_id, Character.name == name)
    ).scalar_one_or_none()
    if ch:
        return ch
    alias_row = session.execute(
        select(Alias).where(Alias.project_id == project_id, Alias.alias == name)
    ).scalar_one_or_none()
    if alias_row:
        ch = session.get(Character, alias_row.entity_id)
        if ch is not None and ch.project_id == project_id:
            return ch
    return None


def get_all_characters(session: Session, project_id: uuid.UUID) -> list[Character]:
    return list(session.execute(
        select(Character).where(Character.project_id == project_id).order_by(Character.name)
    ).scalars())


def get_hard_facts(session: Session, project_id: uuid.UUID, chapter_seq: int | None = None) -> list[Fact]:
    """硬约束恒在 Top-K（§7.2）+ 当前有效普通事实。"""
    q = select(Fact).where(Fact.project_id == project_id, Fact.confirm_status == "confirmed")
    if chapter_seq is not None:
        q = q.where(
            (Fact.is_hard.is_(True)) | (Fact.valid_from <= chapter_seq) & (Fact.valid_to.is_(None))
        )
    else:
        q = q.where((Fact.is_hard.is_(True)) | (Fact.valid_to.is_(None)))
    # 硬约束全部保留；超预算由提示词组装显式拒绝，不能被 LIMIT 静默丢弃。
    hard = list(session.execute(q.where(Fact.is_hard.is_(True)).order_by(Fact.created_at.desc())).scalars())
    soft = list(session.execute(q.where(Fact.is_hard.is_(False)).order_by(Fact.created_at.desc()).limit(100)).scalars())
    return hard + soft


def get_recent_events(session: Session, project_id: uuid.UUID, limit: int = 20,
                      *, before_chapter: int | None = None) -> list[Event]:
    query = select(Event).where(Event.project_id == project_id)
    if before_chapter is not None:
        query = query.where(Event.source_chapter < before_chapter)
    return list(session.execute(
        query.order_by(Event.source_chapter.desc()).limit(limit)
    ).scalars())


def get_character_state(session: Session, project_id: uuid.UUID, character_id: uuid.UUID,
                        chapter_seq: int) -> dict[str, str]:
    """人物状态台账当前值：按 chapter_seq 最近一条有效记录物化（§7.7）。

    valid_to IS NULL 过滤已失效状态（§7.3 章节重写后旧状态 valid_to 关闭，不再计入）；
    valid_from <= chapter_seq 与 get_hard_facts 时间窗口径对齐（§7.7 时序快照语义）。
    """
    rows = session.execute(
        select(CharacterState).where(
            CharacterState.project_id == project_id,
            CharacterState.character_id == character_id,
            CharacterState.chapter_seq <= chapter_seq,
            CharacterState.valid_from <= chapter_seq,
            CharacterState.valid_to.is_(None),
        ).order_by(CharacterState.chapter_seq.desc())
    ).scalars().all()
    state: dict[str, str] = {}
    for r in rows:  # 同一 field 只取最近一条
        if r.field not in state:
            state[r.field] = r.new_value or ""
    return state


def get_relations(session: Session, project_id: uuid.UUID, entity_ids: list[uuid.UUID] | None = None) -> list[Relation]:
    q = select(Relation).where(Relation.project_id == project_id, Relation.valid_to.is_(None))
    if entity_ids:
        q = q.where(Relation.source_id.in_(entity_ids) | Relation.target_id.in_(entity_ids))
    return list(session.execute(q).scalars())


def get_relation_current_type(session: Session, project_id: uuid.UUID,
                              source_id: uuid.UUID, target_id: uuid.UUID) -> str | None:
    """有序对当前关系类型（§7.8 当前关系 = 最新 valid_to IS NULL）。

    L2 正文-台账语义比对（§8.6）的台账侧取值：0 或 >1 条活跃行（无记录 /
    存量重复/矛盾）→ None，歧义交 L1 relation_ledger_check 兜底，不在此吞掉。
    """
    rows = session.execute(
        select(Relation).where(
            Relation.project_id == project_id,
            Relation.source_id == source_id,
            Relation.target_id == target_id,
            Relation.valid_to.is_(None),
        )
    ).scalars().all()
    if len(rows) != 1:
        return None
    return rows[0].relation_type


def get_open_foreshadows(session: Session, project_id: uuid.UUID) -> list[Foreshadow]:
    return list(session.execute(
        select(Foreshadow).where(Foreshadow.project_id == project_id, Foreshadow.status.in_(["planted", "developing"]))
    ).scalars())


def get_plot_threads(session: Session, project_id: uuid.UUID) -> list[PlotThread]:
    return list(session.execute(
        select(PlotThread).where(PlotThread.project_id == project_id, PlotThread.status == "active")
    ).scalars())


def get_active_lessons(session: Session, project_id: uuid.UUID) -> list[WritingLesson]:
    """在效写作经验（reflexion 注入用，§8.9）：复发数降序、最近优先，cap 在 recall 层。"""
    return list(session.execute(
        select(WritingLesson)
        .where(WritingLesson.project_id == project_id, WritingLesson.status == "active")
        .order_by(WritingLesson.recurrence_count.desc(), WritingLesson.created_at.desc())
    ).scalars())


def get_entities(session: Session, project_id: uuid.UUID) -> list[Entity]:
    """设定实体（§7.11 ④ 自动建档：武器/功法/技能/地点），创建时间倒序。

    排序在取数层固定，recall 的相关度重排建立在此顺序之上（未命中场景地点名的按此补足）。
    """
    return list(session.execute(
        select(Entity).where(Entity.project_id == project_id)
        .order_by(Entity.created_at.desc())
    ).scalars())


def get_chapter(session: Session, project_id: uuid.UUID, chapter_seq: int) -> Chapter | None:
    return session.execute(
        select(Chapter).where(Chapter.project_id == project_id, Chapter.chapter_seq == chapter_seq)
    ).scalar_one_or_none()


def ensure_chapter_placeholder(session: Session, *, project_id: uuid.UUID,
                               chapter_seq: int, status: str = "writing") -> Chapter:
    """在工作流开始时物化目标章节，让正文、流转和审核从一开始就有明确归属。"""
    if status not in ("planning", "writing"):
        raise ValueError(f"非法章节占位状态: {status}")
    chapter = session.execute(
        select(Chapter).where(
            Chapter.project_id == project_id,
            Chapter.chapter_seq == chapter_seq,
        ).with_for_update()
    ).scalar_one_or_none()
    if chapter is None:
        chapter = Chapter(
            project_id=project_id,
            chapter_seq=chapter_seq,
            status=status,
            generation_source="auto",
            version=1,
        )
        session.add(chapter)
        session.flush()
    elif chapter.content is None and chapter.status in (
        "planning", "writing", "failed", "cancelled",
    ):
        chapter.status = status
        chapter.generation_source = "auto"
    return chapter


def get_latest_chapter(session: Session, project_id: uuid.UUID) -> Chapter | None:
    return session.execute(
        select(Chapter).where(Chapter.project_id == project_id)
        .order_by(Chapter.chapter_seq.desc()).limit(1)
    ).scalar_one_or_none()


def get_chapter_outline(session: Session, project_id: uuid.UUID, chapter_seq: int) -> ChapterOutline | None:
    return session.execute(
        select(ChapterOutline).where(ChapterOutline.project_id == project_id, ChapterOutline.chapter_seq == chapter_seq)
    ).scalar_one_or_none()


def get_volume_outline(session: Session, project_id: uuid.UUID, volume_seq: int) -> VolumeOutline | None:
    return session.execute(
        select(VolumeOutline).where(VolumeOutline.project_id == project_id, VolumeOutline.volume_seq == volume_seq)
    ).scalar_one_or_none()


# ---- 写（仅 persist / 编排层调用，§6.2 数据流边界）----

def snapshot_chapter(session: Session, chapter: Chapter, reason: str = "edit") -> None:
    """覆盖写前快照：把 chapter 当前状态写入 chapter_versions（阶段 4 版本表）。

    版本表语义：chapters 是「当前版本」，每次写前把旧状态留痕成历史行（含当时版本号），
    回退/审计据此恢复。仅对已存在章节（有 id）调用——新建章无旧状态可快照。
    """
    if chapter is None or chapter.id is None:
        return
    session.add(ChapterVersion(
        project_id=chapter.project_id,
        chapter_id=chapter.id,
        version=chapter.version or 1,
        title=chapter.title,
        content=chapter.content,
        summary=chapter.summary,
        reason=reason,
    ))


def save_review_draft(session: Session, *, project_id: uuid.UUID, chapter_seq: int,
                      content: str, summary: str | None = None,
                      title: str | None = None) -> Chapter:
    """保存可见的待确认正文；人工确认只控制设定入库，不隐藏已经生成的文章。"""
    chapter = session.execute(
        select(Chapter).where(Chapter.project_id == project_id,
                              Chapter.chapter_seq == chapter_seq).with_for_update()
    ).scalar_one_or_none()
    if chapter is None:
        chapter = Chapter(project_id=project_id, chapter_seq=chapter_seq,
                          status="awaiting_review", version=1)
        session.add(chapter)
    elif chapter.content != content and chapter.content is not None:
        snapshot_chapter(session, chapter, reason="auto_review_draft")
        chapter.version = (chapter.version or 0) + 1
    chapter.content = content
    chapter.summary = summary
    if title is not None:
        chapter.title = title
    chapter.status = "awaiting_review"
    chapter.generation_source = "auto"
    return chapter


def save_chapter(session: Session, *, project_id: uuid.UUID, chapter_seq: int, content: str,
                 summary: str | None = None, title: str | None = None,
                 generation_source: str = "manual") -> Chapter:
    # 行锁（评审 M2）：与用户编辑端点（PUT content / restore）串行化同一章的覆盖写，
    # 防版本表重复 (chapter_id, version) 行 + 丢失更新（唯一约束为最终兜底）。
    chapter = session.execute(
        select(Chapter).where(Chapter.project_id == project_id,
                              Chapter.chapter_seq == chapter_seq).with_for_update()
    ).scalar_one_or_none()
    if chapter is None:
        chapter = Chapter(project_id=project_id, chapter_seq=chapter_seq, status="confirmed", version=1)
        session.add(chapter)
    elif not (chapter.status == "awaiting_review" and chapter.content == content):
        # 待确认正文已经对用户可见；最终确认同一稿只切状态，不制造一份重复历史版本。
        snapshot_chapter(session, chapter, reason=generation_source or "edit")  # 版本表快照旧状态
        chapter.version = (chapter.version or 0) + 1
    chapter.content = content
    chapter.summary = summary
    chapter.title = title
    chapter.status = "confirmed"
    chapter.generation_source = generation_source
    return chapter
