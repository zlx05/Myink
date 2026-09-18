"""用户 / 作品 / 设定 / 人物 / 对话模型（plan.md §11.1）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from myink.models.base import Base, TenantMixin, TimestampMixin, UUIDPkMixin


class User(Base, UUIDPkMixin, TimestampMixin):
    """用户与配额（§13 三层闸门之一：每用户每日配额）。"""

    __tablename__ = "users"

    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    # 阶段 6：用户等级（VIP → 网关入队 RabbitMQ 高优先级；老库由 db.ensure_user_tier 幂等补齐）
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="normal", server_default="normal",
        comment="用户等级：normal/vip（VIP 任务高优先级入队）",
    )
    daily_quota: Mapped[int] = mapped_column(Integer, default=2, nullable=False, comment="每日章节配额")
    concurrent_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="进行中任务上限")
    # 账号级环境配置（模型连接/路由 + MCP 扫榜）；老库由 db.ensure_user_environment 幂等补列
    environment: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False, server_default=text("'{}'"),
        comment="账号级环境配置（模型连接/路由 + MCP 扫榜）",
    )


class Project(Base, UUIDPkMixin, TimestampMixin):
    """作品基本信息（genre 等）。"""

    __tablename__ = "projects"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    genre: Mapped[str] = mapped_column(String(64), default="仙侠玄幻", nullable=False)
    target_words: Mapped[int | None] = mapped_column(Integer)
    current_volume: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_chapter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProjectSettings(Base, UUIDPkMixin, TimestampMixin):
    """作品设定：世界观规则 / 文风档案 / 题材 Skill / 模型路由表 / 硬约束（JSONB）。"""

    __tablename__ = "project_settings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    world_rules: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False, comment="世界观规则（§7.11）")
    style_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False, comment="文风档案 StyleProfile（§7.12）")
    skill_pack: Mapped[str | None] = mapped_column(String(64), comment="文风种子标记（旧字段，不再作题材包）")
    genre_pack: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False, comment="本书题材包快照（建书深拷贝，只改本书）",
    )
    model_routes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False, comment="role→model 路由表（§6.10）")
    hard_constraints: Mapped[list] = mapped_column(JSON, default=list, nullable=False, comment="硬约束（§7.10）")
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="乐观版本号（§7.6）")


class Character(Base, UUIDPkMixin, TimestampMixin):
    """人物静态基底（长期事实，不含易变状态）。"""

    __tablename__ = "characters"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    race: Mapped[str | None] = mapped_column(String(64))
    origin: Mapped[str | None] = mapped_column(String(255), comment="出身")
    realm_cap: Mapped[str] = mapped_column(String(64), nullable=False, comment="境界上限（战力硬约束）")
    personality: Mapped[str | None] = mapped_column(Text, comment="性格基调（人设漂移审计基线 §8.6）")
    base_attrs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Faction(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "factions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    stance: Mapped[str | None] = mapped_column(String(255), comment="立场/主张")
    resources: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class Location(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "locations"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, comment="层级父地点")


class Conversation(Base, UUIDPkMixin, TimestampMixin):
    """创作对话（§7.1 会话记忆，RLS 隔离）。"""

    __tablename__ = "conversations"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(255))


class Message(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="user/assistant/system")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, comment="内容摘要（供召回）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
