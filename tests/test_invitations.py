"""Invitation-gated account registration."""

from __future__ import annotations

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from typer.testing import CliRunner

from myink.api.main import app
from myink.cli import app as cli_app
from myink.config import settings
from myink.db import get_admin_engine, new_session
from myink.invitations import create_invitation, hash_invitation_token
from myink.models import Invitation, User

client = TestClient(app)
runner = CliRunner()
TEST_JWT_SECRET = "test-jwt-secret-at-least-32-bytes-long"


@pytest.fixture(autouse=True)
def _invitation_test_schema(monkeypatch):
    import myink.api.auth as auth

    monkeypatch.setattr(auth, "settings", replace(settings, jwt_secret=TEST_JWT_SECRET))
    Invitation.__table__.create(get_admin_engine(), checkfirst=True)


def _issue(*, expires_at: datetime | None = None, max_redemptions: int = 1) -> tuple[uuid.UUID, str]:
    with new_session() as db:
        invitation, token = create_invitation(
            db,
            expires_at=expires_at or datetime.now(timezone.utc) + timedelta(days=1),
            max_redemptions=max_redemptions,
        )
        db.commit()
        return invitation.id, token


def _register(username: str, token: str | None):
    body = {"username": username, "password": "correct horse battery 1"}
    if token is not None:
        body["invitation_code"] = token
    return client.post("/internal/v1/auth/register", json=body)


def _cleanup(*, usernames: list[str] = [], invitation_ids: list[uuid.UUID] = []) -> None:
    with new_session() as db:
        if usernames:
            db.execute(delete(User).where(User.username.in_(usernames)))
        if invitation_ids:
            db.execute(delete(Invitation).where(Invitation.id.in_(invitation_ids)))
        db.commit()


def test_registration_requires_an_invitation_code():
    username = f"missing-invite-{uuid.uuid4().hex[:8]}"
    response = _register(username, None)
    assert response.status_code == 403
    assert response.json() == {"detail": "INVITATION_REQUIRED"}
    with new_session() as db:
        assert db.scalar(select(User.id).where(User.username == username)) is None


def test_registration_rejects_an_unknown_invitation_without_creating_user():
    username = f"bad-invite-{uuid.uuid4().hex[:8]}"
    response = _register(username, "not-a-real-invitation")
    assert response.status_code == 403
    assert response.json() == {"detail": "INVITATION_INVALID"}
    with new_session() as db:
        assert db.scalar(select(User.id).where(User.username == username)) is None


def test_registration_rejects_expired_and_revoked_invitations():
    expired_id, expired = _issue()
    revoked_id, revoked = _issue()
    with new_session() as db:
        expired_row = db.get(Invitation, expired_id)
        assert expired_row is not None
        expired_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        row = db.get(Invitation, revoked_id)
        assert row is not None
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()
    expired_user = f"expired-{uuid.uuid4().hex[:8]}"
    revoked_user = f"revoked-{uuid.uuid4().hex[:8]}"
    try:
        expired_response = _register(expired_user, expired)
        revoked_response = _register(revoked_user, revoked)
        assert expired_response.status_code == 403
        assert expired_response.json() == {"detail": "INVITATION_EXPIRED"}
        assert revoked_response.status_code == 403
        assert revoked_response.json() == {"detail": "INVITATION_REVOKED"}
    finally:
        _cleanup(
            usernames=[expired_user, revoked_user],
            invitation_ids=[expired_id, revoked_id],
        )


def test_invitation_is_single_use_and_plaintext_is_never_stored():
    invitation_id, token = _issue()
    first_user = f"invited-{uuid.uuid4().hex[:8]}"
    second_user = f"reused-{uuid.uuid4().hex[:8]}"
    try:
        first = _register(first_user, token)
        second = _register(second_user, token)
        assert first.status_code == 201, first.text
        assert second.status_code == 403
        assert second.json() == {"detail": "INVITATION_USED"}
        with new_session() as db:
            invitation = db.get(Invitation, invitation_id)
            assert invitation is not None
            assert invitation.token_digest == hash_invitation_token(token)
            assert token not in invitation.token_digest
            assert invitation.redemption_count == 1
            assert db.scalar(select(User.id).where(User.username == second_user)) is None
    finally:
        _cleanup(usernames=[first_user, second_user], invitation_ids=[invitation_id])


def test_concurrent_redemption_allows_exactly_one_account():
    invitation_id, token = _issue()
    usernames = [f"race-a-{uuid.uuid4().hex[:8]}", f"race-b-{uuid.uuid4().hex[:8]}"]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda name: _register(name, token), usernames))
        assert sorted(response.status_code for response in responses) == [201, 403]
        rejected = next(response for response in responses if response.status_code == 403)
        assert rejected.json() == {"detail": "INVITATION_USED"}
        with new_session() as db:
            assert len(list(db.scalars(select(User.id).where(User.username.in_(usernames))))) == 1
            invitation = db.get(Invitation, invitation_id)
            assert invitation is not None
            assert invitation.redemption_count == 1
    finally:
        _cleanup(usernames=usernames, invitation_ids=[invitation_id])


def test_duplicate_username_rolls_back_redemption_for_later_use():
    first_invitation_id, first_token = _issue()
    retry_invitation_id, retry_token = _issue()
    existing = f"rollback-{uuid.uuid4().hex[:8]}"
    replacement = f"replacement-{uuid.uuid4().hex[:8]}"
    try:
        assert _register(existing, first_token).status_code == 201
        duplicate = _register(existing, retry_token)
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": "USERNAME_TAKEN"}
        successful_retry = _register(replacement, retry_token)
        assert successful_retry.status_code == 201, successful_retry.text
        with new_session() as db:
            invitation = db.get(Invitation, retry_invitation_id)
            assert invitation is not None
            assert invitation.redemption_count == 1
    finally:
        _cleanup(
            usernames=[existing, replacement],
            invitation_ids=[first_invitation_id, retry_invitation_id],
        )


def test_response_construction_failure_rolls_back_user_and_redemption(monkeypatch):
    invitation_id, token = _issue()
    username = f"response-failure-{uuid.uuid4().hex[:8]}"
    try:
        import myink.api.auth as auth

        with monkeypatch.context() as patch:
            patch.setattr(
                auth,
                "_auth_response",
                lambda _user: (_ for _ in ()).throw(RuntimeError("response failed")),
            )
            with pytest.raises(RuntimeError, match="response failed"):
                _register(username, token)

        with new_session() as db:
            assert db.scalar(select(User.id).where(User.username == username)) is None
            invitation = db.get(Invitation, invitation_id)
            assert invitation is not None
            assert invitation.redemption_count == 0

        retry = _register(username, token)
        assert retry.status_code == 201, retry.text
    finally:
        _cleanup(usernames=[username], invitation_ids=[invitation_id])


def test_cli_creates_256_bit_default_invitation_and_revokes_by_id():
    created = runner.invoke(cli_app, ["create-invite"])
    assert created.exit_code == 0, created.output
    token_match = re.search(r"Invitation code: ([A-Za-z0-9_-]{43})\b", created.output)
    id_match = re.search(r"Invitation id: ([0-9a-f-]{36})\b", created.output)
    assert token_match is not None
    assert id_match is not None
    token = token_match.group(1)
    invitation_id = uuid.UUID(id_match.group(1))
    try:
        with new_session() as db:
            invitation = db.get(Invitation, invitation_id)
            assert invitation is not None
            assert invitation.token_digest == hash_invitation_token(token)
            assert invitation.max_redemptions == 1
            remaining = invitation.expires_at - datetime.now(timezone.utc)
            assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)

        revoked = runner.invoke(cli_app, ["revoke-invite", str(invitation_id)])
        assert revoked.exit_code == 0, revoked.output
        assert token not in revoked.output
        with new_session() as db:
            invitation = db.get(Invitation, invitation_id)
            assert invitation is not None
            assert invitation.revoked_at is not None
    finally:
        _cleanup(invitation_ids=[invitation_id])
