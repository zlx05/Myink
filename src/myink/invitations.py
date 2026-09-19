"""Issue and atomically redeem invitation credentials."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from myink.models.invitation import Invitation


class InvitationRejected(ValueError):
    """A stable API error code for a non-redeemable invitation."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def hash_invitation_token(token: str) -> str:
    """Return the storage representation without retaining the plaintext token."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def create_invitation(
    db: Session,
    *,
    expires_at: datetime,
    max_redemptions: int = 1,
) -> tuple[Invitation, str]:
    """Add a new invitation and return its one-time plaintext for explicit display."""
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    if expires_at <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    if max_redemptions < 1:
        raise ValueError("max_redemptions must be positive")
    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        token_digest=hash_invitation_token(token),
        expires_at=expires_at,
        max_redemptions=max_redemptions,
        redemption_count=0,
    )
    db.add(invitation)
    db.flush()
    return invitation, token


def _load_redeemable(
    db: Session,
    token: str | None,
    *,
    now: datetime,
    lock: bool,
) -> Invitation:
    normalized = token.strip() if token else ""
    if not normalized:
        raise InvitationRejected("INVITATION_REQUIRED")
    statement = select(Invitation).where(
        Invitation.token_digest == hash_invitation_token(normalized)
    )
    if lock:
        statement = statement.with_for_update()
    invitation = db.execute(statement).scalar_one_or_none()
    if invitation is None:
        raise InvitationRejected("INVITATION_INVALID")
    if invitation.revoked_at is not None:
        raise InvitationRejected("INVITATION_REVOKED")
    if invitation.expires_at <= now:
        raise InvitationRejected("INVITATION_EXPIRED")
    if invitation.redemption_count >= invitation.max_redemptions:
        raise InvitationRejected("INVITATION_USED")
    return invitation


def validate_invitation(db: Session, token: str | None, *, now: datetime | None = None) -> None:
    """Cheap preflight validation before performing an expensive password hash."""
    _load_redeemable(db, token, now=now or datetime.now(timezone.utc), lock=False)


def consume_invitation(
    db: Session,
    token: str | None,
    *,
    now: datetime | None = None,
) -> Invitation:
    """Lock and consume one redemption inside the caller's account transaction."""
    invitation = _load_redeemable(
        db,
        token,
        now=now or datetime.now(timezone.utc),
        lock=True,
    )
    invitation.redemption_count += 1
    return invitation


def revoke_invitation(
    db: Session,
    invitation_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> bool:
    invitation = db.get(Invitation, invitation_id, with_for_update=True)
    if invitation is None:
        return False
    if invitation.revoked_at is None:
        invitation.revoked_at = now or datetime.now(timezone.utc)
    return True


def ensure_invitation_schema() -> None:
    """Add the standalone invitation table to an existing installation."""
    from myink.db import get_admin_engine

    Invitation.__table__.create(get_admin_engine(), checkfirst=True)
