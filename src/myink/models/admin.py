"""Append-only read audit. A target string deliberately avoids project RLS."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from myink.models.base import Base


class AdminAccessLog(Base):
    __tablename__ = "admin_access_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def ensure_admin_schema() -> None:
    from myink.db import get_admin_engine
    Base.metadata.create_all(get_admin_engine(), tables=[AdminAccessLog.__table__])
