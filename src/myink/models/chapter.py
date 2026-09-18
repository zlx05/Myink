"""章节与大纲模型（§11.1）。"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from myink.models.base import Base, TimestampMixin, UUIDPkMixin

CHAPTER_STATUSES = ("planning", "writing", "awaiting_review", "confirmed", "failed", "cancelled")


class Chapter(Base, UUIDPkMixin, TimestampMixin):
    """章节正文与状态。"""

    __tablename__ = "chapters"
    __table_args__ = (
        # (project_id, chapter_seq) 唯一（评审 A6）：save_chapter 是查后 upsert，
        # 并发入口 / CLI 直调绕过 worker 书锁时靠约束兜底，杜绝同章双行竞态
        UniqueConstraint("project_id", "chapter_seq", name="uq_chapters_project_seq"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, comment="章节摘要")
    status: Mapped[str] = mapped_column(String(16), default="planning", nullable=False)
    generation_source: Mapped[str | None] = mapped_column(String(32), comment="manual/batch/revise")
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ChapterVersion(Base, UUIDPkMixin, TimestampMixin):
    """章节历史版本（阶段 4 版本表：覆盖写前快照，供历史查看/回退/审计）。

    语义：chapters 行是「当前版本」（version = 实时计数），本表每行是「某个历史版本」
    ——每次覆盖写正文前把旧状态快照成一行（reason 记录来源 manual/batch/revise/revert），
    回退即把目标版本内容写回 chapters 并再快照当前（回退本身留痕）。
    """

    __tablename__ = "chapter_versions"
    __table_args__ = (
        # 单章历史按 version 倒序扫（versions 列表端点）
        Index("ix_chapter_versions_chapter_version", "chapter_id", "version"),
        # 同章版本号唯一兜底（评审 M2）：并发写（用户编辑 vs 批次 persist）即使读到同一
        # version 各自快照，约束也保证不落重复行——重复插入一方 IntegrityError 回滚。
        UniqueConstraint("chapter_id", "version", name="uq_chapter_versions_chapter_version"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(32), default="edit", nullable=False,
                                       comment="manual/batch/revise/revert")


class VolumeOutline(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "volume_outlines"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    volume_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    outline: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False, comment="卷纲内容")
