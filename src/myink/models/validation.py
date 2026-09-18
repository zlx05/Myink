"""校验报告与发现（§8，L1/L2 统一，evidence 证据链）。"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from myink.models.base import Base, TimestampMixin, UUIDPkMixin


class ValidationReport(Base, UUIDPkMixin, TimestampMixin):
    """校验报告（含 summary 汇总）。"""

    __tablename__ = "validation_reports"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="修订轮次")
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False, comment="{total,critical,resolved}")


class Finding(Base, UUIDPkMixin, TimestampMixin):
    """校验发现（conflict_key 跨修订轮稳定，§6.4）。"""

    __tablename__ = "findings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="hash(类型+实体+位置)")
    conflict_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, comment="critical/major/minor/hint")
    scope: Mapped[str] = mapped_column(String(16), default="local", nullable=False)
    source: Mapped[str] = mapped_column(String(4), default="L1", nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False, comment="[{chapter,quote}]")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True, comment="仅 L2 需要")
