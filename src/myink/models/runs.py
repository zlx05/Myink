"""任务与运行记录模型。

agent_runs 每节点一行全字段（§6.8 任务成本透明）：
节点、角色、model_id（含降级后实际）、输入/输出 token（思考 token 计入输出）、
缓存命中标记、耗时、成本估算、重试、降级、错误 —— 前端节点时间线数据源。
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from myink.models.base import Base, TimestampMixin, UUIDPkMixin

TASK_STATUSES = ("queued", "running", "paused", "awaiting_plan", "awaiting_review", "failed", "cancelled", "done")
TASK_TYPES = ("chapter_generate", "batch_generate", "validate", "outline_generate")


class Task(Base, UUIDPkMixin, TimestampMixin):
    """异步任务（队列最终态由 DB 承载，Redis 只放可重建数据，§5.3/§6.12）。"""

    __tablename__ = "tasks"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(128), comment="request_id→task_id→thread_id 链路")
    batch_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, comment="批次归属（thread_id=batch_task_id）")
    chapter_seq: Mapped[int | None] = mapped_column(Integer, comment="单章任务的目标章节")


class AgentRun(Base, TimestampMixin):
    """每节点执行记录（§6.8）。id 用自增（观测数据，无租户语义，不参与 RLS 查询热点）。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        # 任务详情/成本聚合（§6.8）：全部查询按 task_id 前缀 + id 增量扫（routes_tasks/observer/
        # processor 无一条按 project_id 查——观测表无 RLS，故组合索引前缀用 task_id 而非 project_id）。
        # 前缀 LIKE 在默认 collation 下走 btree 范围扫；若将来 collation 非 C 需换 pg_trgm/text_pattern_ops。
        Index("ix_agent_runs_task_id", "task_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # thread_id（单章=task_id；批次内= batch_task_id:ch{seq}），字符串标识，非严格 uuid
    # 不设 index=True：__table_args__ 组合索引 ix_agent_runs_task_id(task_id,id) 左前缀已覆盖
    # 按 task_id 查询；同名单列索引会在 create_all fresh 库时与组合索引同名冲突（DuplicateTable）
    task_id: Mapped[str | None] = mapped_column(String(128))
    node: Mapped[str] = mapped_column(String(64), nullable=False, comment="load_state/recall/plan_chapter/write/extract/validate/revise/persist/batch_plan/...")
    role: Mapped[str | None] = mapped_column(String(32), comment="Planner/Writer/Memory/Validator")
    model_id: Mapped[str | None] = mapped_column(String(64), comment="含降级后的实际模型")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="思考 token 计入输出")
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="缓存命中（价差 50 倍）")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_est: Mapped[float] = mapped_column(Float, default=0.0, nullable=False, comment="成本估算（¥）")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # 节点关键产物（debug 回放，§6.8）：audit 行→audit_verdict；write/audit 工具轮→tool_trace；
    # 确定性节点（recall/validate/persist/load_state）→执行统计。纯观测字段，不影响执行语义。
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
