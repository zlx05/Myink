"""三层记忆 / 图谱模型（plan.md §11.1，字段对齐 spec/schema.md）。"""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from myink.models.base import Base, TimestampMixin, UUIDPkMixin

# 关键枚举常量（与 spec/schema.md / plan.md §6.4/§7/§8 一致，DB 层 CheckConstraint 防坏数据）
CHARACTER_STATE_FIELDS = ("location", "injury", "realm", "power", "item", "knowledge", "goal", "identity", "alive")
RELATION_TYPES = ("hostile", "ally", "master_student", "located_in", "owns", "defeated_by", "knows", "promises", "happened_at")
FORESHADOW_STATUSES = ("planted", "developing", "resolved", "dropped")
PLOT_THREAD_KINDS = ("main", "side")
PLOT_THREAD_STATUSES = ("active", "stalled", "closed")
FACT_CONFIRM_STATUSES = ("pending", "confirmed", "rejected", "expired")
# character_card（§7.11 ④ 新人物卡片候选，高风险需人工确认）/ new_entity（新设定实体
# 候选，低风险自动建档）——两者都进 extract 产出；character_card 进待确认池，new_entity
# 由 persist 自动落 entities（仿 plotline 不进池口径），CHECK 带上防路由漂移。
CANDIDATE_KINDS = ("event", "fact", "character_state", "relation_change", "foreshadow",
                   "chapter_summary", "memory_removal", "character_card", "new_entity")
CANDIDATE_STATUSES = ("pending", "confirmed", "rejected")


class CharacterState(Base, UUIDPkMixin, TimestampMixin):
    """人物状态台账（追加式，只追加不覆盖，当前状态 = 按 chapter_seq 最近有效行，§7.7）。"""

    __tablename__ = "character_states"
    __table_args__ = (
        CheckConstraint(f"field IN {CHARACTER_STATE_FIELDS}", name="field_enum"),
        # 台账物化（§7.7）：按 project+character 拉最近章序列 → 组合索引前缀命中
        Index("ix_character_states_project_char_seq",
              "project_id", "character_id", "chapter_seq"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    chapter_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    valid_from: Mapped[int | None] = mapped_column(Integer, default=1)
    valid_to: Mapped[int | None] = mapped_column(Integer)


class Fact(Base, UUIDPkMixin, TimestampMixin):
    """长期事实（§7.3，valid_from/valid_to 支持失效）。"""

    __tablename__ = "facts"
    __table_args__ = (
        CheckConstraint(f"confirm_status IN {FACT_CONFIRM_STATUSES}", name="confirm_status_enum"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), comment="世界观/身份/归属/关系/规则")
    is_hard: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="硬约束恒在 Top-K")
    source_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    confirm_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    valid_from: Mapped[int | None] = mapped_column(Integer, default=1)
    valid_to: Mapped[int | None] = mapped_column(Integer)


class Event(Base, UUIDPkMixin, TimestampMixin):
    """剧情事件（中期记忆，§7.4）。"""

    __tablename__ = "events"
    __table_args__ = (
        # recall 近章事件（§7.4）：ORDER BY source_chapter DESC LIMIT n —— btree 双向，
        # ASC 索引反向扫即服务 DESC 排序，无需显式 desc（与 character_states 同理）
        Index("ix_events_project_chapter", "project_id", "source_chapter"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    participants: Mapped[list] = mapped_column(JSON, default=list, nullable=False, comment="归一化 canonical id")
    source_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class Relation(Base, UUIDPkMixin, TimestampMixin):
    """实体关系（图谱，带时间窗，§7.8/§9）。"""

    __tablename__ = "relations"
    __table_args__ = (
        CheckConstraint(f"relation_type IN {RELATION_TYPES}", name="relation_type_enum"),
        # 1-2 跳关系查询（§7.8）：get_relations 按 source_id IN / target_id IN 双向查
        # （过滤 project_id + valid_to IS NULL），两枚索引各覆盖一端；不按 relation_type
        # 过滤故不进索引（评审建议的 relation_type/valid_to 列是空列，不建空索引）。
        Index("ix_relations_project_source", "project_id", "source_id"),
        Index("ix_relations_project_target", "project_id", "target_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    properties: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    source_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[int | None] = mapped_column(Integer, default=1)
    valid_to: Mapped[int | None] = mapped_column(Integer)


class Entity(Base, UUIDPkMixin, TimestampMixin):
    """图谱实体（人物/势力/地点/物品/能力/事件，§9）。"""

    __tablename__ = "entities"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(128), nullable=False)
    properties: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Alias(Base, UUIDPkMixin, TimestampMixin):
    """实体别名 → canonical id（§7.5 归一化）。"""

    __tablename__ = "aliases"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)


class Foreshadow(Base, UUIDPkMixin, TimestampMixin):
    """伏笔状态机（§7.9）。"""

    __tablename__ = "foreshadows"
    __table_args__ = (
        CheckConstraint(f"status IN {FORESHADOW_STATUSES}", name="status_enum"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    planted_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_chapter: Mapped[int | None] = mapped_column(Integer)
    trigger: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False, comment="回收条件：触发者+动作+对象")
    last_touched: Mapped[int | None] = mapped_column(Integer, comment="最近推进章节（回收压力）")


class PlotThread(Base, UUIDPkMixin, TimestampMixin):
    """剧情线（线程债务治理，§7.9/§8.6）。"""

    __tablename__ = "plot_threads"
    __table_args__ = (
        CheckConstraint(f"kind IN {PLOT_THREAD_KINDS}", name="kind_enum"),
        CheckConstraint(f"status IN {PLOT_THREAD_STATUSES}", name="status_enum"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_progress_chapter: Mapped[int | None] = mapped_column(Integer)


class MemoryCandidate(Base, UUIDPkMixin, TimestampMixin):
    """待确认记忆候选（DB 为最终权威，§7.3）。"""

    __tablename__ = "memory_candidates"
    __table_args__ = (
        CheckConstraint(f"kind IN {CANDIDATE_KINDS}", name="kind_enum"),
        CheckConstraint(f"status IN {CANDIDATE_STATUSES}", name="status_enum"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, comment="对应 kind 的对象体")
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    review: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EmbeddingRow(Base, UUIDPkMixin, TimestampMixin):
    """向量对象（pgvector，分层：世界观/事件，§11.1）。

    level 只有 world（软事实）与 event（事件）两个写入者——硬约束恒在 Top-K 不向量化（§7.2）。
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        # 显式 filter 参与 ANN 扫描（§14.1 坑 1）：按 project+level 过滤召回集
        Index("ix_embeddings_project_level", "project_id", "level"),
        # ANN 索引（§5.2，pgvector ≥0.5）：百万级向量避免全表余弦排序；
        # 显式 WHERE project_id 使扫描一开始限定本租户向量集（§14.1 坑 1）。
        Index(
            "ix_embeddings_embedding_hnsw", "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, comment="world/event")
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, comment="来源对象 id")
    source_chapter: Mapped[int | None] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # bge-m3 1024 维（plan.md §5.2 在 pgvector 维度上限内）
    embedding: Mapped[list] = mapped_column(Vector(1024), nullable=False)


# 写作经验生命周期（reflexion 复盘沉淀，§8.9）：proposed（高危待确认）→ active / rejected
WRITING_LESSON_STATUSES = ("proposed", "active", "rejected")
# 注入通道：planning（plan_messages） / writing（write_messages） / both
LESSON_TYPES = ("planning", "writing", "both")


class WritingLesson(Base, UUIDPkMixin, TimestampMixin):
    """本书写作经验（reflexion 复盘沉淀，plan.md §8.9）。

    把审核中枢 audit 发现的跨章问题提炼为本书可复用经验，注入后续章节的规划/写作。
    category 复用 Finding.conflict_type 枚举（复发率确定性匹配键）；同 category 恒一条
    （有则总结演化 update、无则 create），复发指标跨演化连续。
    """

    __tablename__ = "writing_lessons"
    __table_args__ = (
        CheckConstraint(f"status IN {WRITING_LESSON_STATUSES}", name="status_enum"),
        CheckConstraint(f"lesson_type IN {LESSON_TYPES}", name="lesson_type_enum"),
        # 在效经验列出 / 复发率按 category 匹配（RLS 恒带 project_id 前缀，§14.1）
        Index("ix_writing_lessons_project_status", "project_id", "status"),
        Index("ix_writing_lessons_project_category", "project_id", "category"),
        # 跨批同内容去重（仅 active 在效）：同一本书同一条经验只一条
        # （不设批次唯一约束——一批可提炼多条 lesson；幂等靠 guard + content_hash）
        Index(
            "uq_writing_lessons_active_content", "project_id", "content_hash",
            unique=True, postgresql_where=text("status = 'active'"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="ConflictType 枚举（复发率确定性匹配键）"
    )
    lesson_type: Mapped[str] = mapped_column(
        String(16), default="both", nullable=False, comment="注入通道 planning/writing/both"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="写作经验正文（注入 plan/write system 段）")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="sha256(content)，跨批去重")
    evidence: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False, comment="[{chapter, conflict_type, severity, quote, suggestion}] 溯源证据"
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    source_chapter: Mapped[int] = mapped_column(Integer, nullable=False, comment="提炼来源最早章节")
    source_batch_task_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="提炼批次 task_id"
    )
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    recurrence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="复发次数（跨演化累计）")
    last_recurrence_at: Mapped[int | None] = mapped_column(Integer, comment="最近复发章节号")


# 全局审计状态枚举（阶段 3 长线治理 L2：completed=成功产出报告 / failed=LLM 失败但已推进 marker）
GLOBAL_AUDIT_STATUSES = ("completed", "failed")
GLOBAL_AUDIT_TRIGGERS = ("batch", "manual")


class GlobalAuditReport(Base, UUIDPkMixin, TimestampMixin):
    """全局审计报告（长线一致性治理 §8.6，阶段 3 L2 抽样审计切片）。

    每 K 章触发一次跨章抽样 L2 审计（人设漂移等），产出可查询的报告 + 证据 findings。
    audited_up_to_chapter 是审计进度 marker（下次窗口从其后开始）；LLM 失败也写
    status=failed 行并推进 marker——非阻塞 + 有界，防每批重审同一毒窗口（§8.6 落地注记）。
    findings 存 Finding 形状 dict（conflict_type/severity/scope/source/evidence），
    前端审计视图与章节 finding 同构渲染。
    """

    __tablename__ = "global_audit_reports"
    __table_args__ = (
        CheckConstraint(f"status IN {GLOBAL_AUDIT_STATUSES}", name="status_enum"),
        CheckConstraint(f"trigger IN {GLOBAL_AUDIT_TRIGGERS}", name="trigger_enum"),
        # 审计进度 marker 查询热键：max(audited_up_to_chapter) WHERE project_id（RLS 恒带前缀）
        Index("ix_global_audit_reports_project_up_to", "project_id", "audited_up_to_chapter"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    window_start: Mapped[int] = mapped_column(Integer, nullable=False, comment="审计窗口起点章号")
    window_end: Mapped[int] = mapped_column(Integer, nullable=False, comment="审计窗口终点章号")
    audited_up_to_chapter: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="审计进度 marker（= window_end，下次窗口从其 +1 开始）"
    )
    trigger: Mapped[str] = mapped_column(String(16), default="batch", nullable=False, comment="batch|manual")
    source_batch_task_id: Mapped[str | None] = mapped_column(String(128), comment="触发批次 task_id（manual 为 NULL）")
    status: Mapped[str] = mapped_column(String(16), default="completed", nullable=False)
    sampled_characters: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False, comment="[{character_id, name}] 抽样角色"
    )
    findings: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False, comment="Finding 形状 dict 列表（persona/hint/local/L2）"
    )
    summary: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False, comment="{sampled, findings, chapters} 计数"
    )
    error: Mapped[str | None] = mapped_column(Text, comment="失败原因（status=failed 时）")
