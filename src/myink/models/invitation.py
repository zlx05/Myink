"""Server-issued credentials that gate new account registration."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from myink.models.base import Base, TimestampMixin, UUIDPkMixin


class Invitation(Base, UUIDPkMixin, TimestampMixin):
    """A revocable, expiring invitation; only its SHA-256 digest is stored."""

    __tablename__ = "invitations"

    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    max_redemptions: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    redemption_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("max_redemptions > 0", name="invitation_max_redemptions_positive"),
        CheckConstraint("redemption_count >= 0", name="invitation_redemption_count_nonnegative"),
        CheckConstraint(
            "redemption_count <= max_redemptions",
            name="invitation_redemption_count_within_limit",
        ),
    )
