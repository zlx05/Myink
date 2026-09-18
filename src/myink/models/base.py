"""SQLAlchemy 声明式基类与公共字段。

所有业务表带 project_id（多租户双键硬隔离，plan.md §14.1），
RLS 策略在 alembic 迁移中创建（FORCE ROW LEVEL SECURITY）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 统一命名约定（alembic autogenerate 友好）
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)


class UUIDPkMixin:
    """UUID 主键（分布式友好，避免自增暴露行数，plan.md §17.1）。"""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """多租户业务表公共列。

    RLS 策略以 project_id 过滤；未设置租户上下文时 fail closed（空结果）。
    """

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, index=True, comment="租户键，RLS 过滤依据"
    )


def tenant_trigger_fn_sql() -> str:
    """RLS 租户取值函数：优先 app.tenant_id，回退 current_user（fail closed 语义）。"""
    return text("""
CREATE OR REPLACE FUNCTION myink.tenant_id()
RETURNS uuid AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$ LANGUAGE sql STABLE;
""")
