"""Account authentication and the private-gateway identity boundary."""

from __future__ import annotations

import uuid
import base64
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, inspect, select, text, update
from typer.testing import CliRunner

from myink.api.main import app
from myink.cli import app as cli_app
from myink.config import settings
from myink.db import get_admin_engine, new_session
from myink.invitations import create_invitation
from myink.models import Invitation, Project, User

client = TestClient(app)
runner = CliRunner()
TEST_JWT_SECRET = "test-jwt-secret-at-least-32-bytes-long"


@pytest.fixture(autouse=True)
def _strong_auth_secret(monkeypatch):
    import myink.api.auth as auth

    monkeypatch.setattr(auth, "settings", replace(settings, jwt_secret=TEST_JWT_SECRET))


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        user = db.execute(select(User).where(User.username == "demo")).scalar_one()
        return user.id


def _trusted_header(user_id: str | uuid.UUID | None) -> dict[str, str]:
    return {"X-Myink-User": str(user_id)} if user_id is not None else {}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _issue_invitation() -> tuple[uuid.UUID, str]:
    with new_session() as db:
        invitation, token = create_invitation(
            db,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.commit()
        return invitation.id, token


@contextmanager
def _registered_account(*, password: str = "correct horse battery 1") -> Iterator[dict]:
    username = f"acct-{uuid.uuid4().hex[:12]}"
    invitation_id, invitation_code = _issue_invitation()
    response = client.post(
        "/internal/v1/auth/register",
        json={"username": username, "password": password, "invitation_code": invitation_code},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    try:
        yield {**data, "password": password}
    finally:
        with new_session() as db:
            db.execute(delete(Project).where(Project.user_id == uuid.UUID(data["user_id"])))
            db.execute(delete(User).where(User.id == uuid.UUID(data["user_id"])))
            db.execute(delete(Invitation).where(Invitation.id == invitation_id))
            db.commit()


def test_register_canonicalizes_username_hashes_password_and_returns_exact_shape():
    suffix = uuid.uuid4().hex[:12]
    submitted = f"  Mixed_User-{suffix.upper()}  "
    canonical = f"mixed_user-{suffix}"
    invitation_id, invitation_code = _issue_invitation()
    response = client.post(
        "/internal/v1/auth/register",
        json={
            "username": submitted,
            "password": "correct horse battery 1",
            "invitation_code": invitation_code,
            "role": "admin",
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    try:
        assert set(data) == {"token", "user_id", "username", "tier", "role", "expires_in"}
        assert data["username"] == canonical
        assert data["tier"] == "normal"
        assert data["role"] == "user"
        assert response.headers["cache-control"] == "no-store"
        claims = jwt.decode(
            data["token"],
            TEST_JWT_SECRET,
            algorithms=["HS256"],
            issuer="myink",
            options={"require": ["sub", "iss", "iat", "exp", "ver"]},
        )
        assert claims["sub"] == data["user_id"]
        assert claims["ver"] == 1

        with new_session() as db:
            user = db.get(User, uuid.UUID(data["user_id"]))
            assert user is not None
            assert user.username == canonical
            assert user.password_hash != "correct horse battery"
            assert user.password_hash.startswith("scrypt$")
            assert user.auth_version == 1
            assert user.tier == "normal"
            assert user.role == "user"
            assert user.environment == {}
            assert db.scalar(select(Project.id).where(Project.user_id == user.id)) is None
    finally:
        with new_session() as db:
            db.execute(delete(User).where(User.id == uuid.UUID(data["user_id"])))
            db.execute(delete(Invitation).where(Invitation.id == invitation_id))
            db.commit()


def test_registration_rejects_duplicate_canonical_username():
    suffix = uuid.uuid4().hex[:12]
    canonical = f"duplicate-{suffix}"
    first_invitation_id, first_invitation_code = _issue_invitation()
    second_invitation_id, second_invitation_code = _issue_invitation()
    first = client.post(
        "/internal/v1/auth/register",
        json={
            "username": f"  {canonical.upper()}  ",
            "password": "correct horse battery 1",
            "role": "admin",
            "invitation_code": first_invitation_code,
        },
    )
    assert first.status_code == 201, first.text
    try:
        second = client.post(
            "/internal/v1/auth/register",
            json={
                "username": canonical,
                "password": "another secure password 2",
                "invitation_code": second_invitation_code,
            },
        )
        assert second.status_code == 409
        assert second.json() == {"detail": "USERNAME_TAKEN"}
    finally:
        with new_session() as db:
            db.execute(delete(User).where(User.id == uuid.UUID(first.json()["user_id"])))
            db.execute(delete(Invitation).where(
                Invitation.id.in_([first_invitation_id, second_invitation_id])
            ))
            db.commit()


@pytest.mark.parametrize("username", ["ab", "has space", "用户名", "Kelvin", "punctuation!"])
def test_registration_rejects_invalid_usernames(username: str):
    invitation_id, invitation_code = _issue_invitation()
    try:
        response = client.post(
            "/internal/v1/auth/register",
            json={
                "username": username,
                "password": "correct horse battery 1",
                "invitation_code": invitation_code,
            },
        )
        assert response.status_code == 422
    finally:
        with new_session() as db:
            db.execute(delete(Invitation).where(Invitation.id == invitation_id))
            db.commit()


@pytest.mark.parametrize("password", ["a1short", "x" * 129, "abcdefgh", "12345678"])
def test_registration_rejects_invalid_new_passwords(password: str):
    invitation_id, invitation_code = _issue_invitation()
    try:
        response = client.post(
            "/internal/v1/auth/register",
            json={
                "username": f"valid-{uuid.uuid4().hex[:10]}",
                "password": password,
                "invitation_code": invitation_code,
            },
        )
        assert response.status_code == 422
    finally:
        with new_session() as db:
            db.execute(delete(Invitation).where(Invitation.id == invitation_id))
            db.commit()


def test_passwordless_unknown_and_wrong_credentials_share_one_unauthorized_response():
    attempts = [
        {"username": "demo", "password": "correct horse battery 1"},
        {"username": f"missing-{uuid.uuid4().hex[:8]}", "password": "correct horse battery 1"},
    ]
    with _registered_account() as account:
        attempts.append({"username": account["username"], "password": "definitely the wrong password"})
        responses = [client.post("/internal/v1/auth/token", json=body) for body in attempts]
    assert [(response.status_code, response.json()) for response in responses] == [
        (401, {"detail": "INVALID_CREDENTIALS"}),
        (401, {"detail": "INVALID_CREDENTIALS"}),
        (401, {"detail": "INVALID_CREDENTIALS"}),
    ]


@pytest.mark.parametrize("password", ["", "x" * 129])
def test_login_rejects_out_of_bounds_passwords_as_invalid_credentials(password: str):
    response = client.post(
        "/internal/v1/auth/token",
        json={"username": "demo", "password": password},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "INVALID_CREDENTIALS"}


def test_auth_hash_capacity_exhaustion_fails_fast():
    import myink.api.auth as auth

    slots = getattr(auth, "_password_slots", None)
    assert slots is not None, "bounded password hashing capacity is missing"
    invitation_id, invitation_code = _issue_invitation()
    assert slots.acquire(blocking=False)
    assert slots.acquire(blocking=False)
    try:
        response = client.post(
            "/internal/v1/auth/register",
            json={
                "username": f"capacity-{uuid.uuid4().hex[:8]}",
                "password": "correct horse battery 1",
                "invitation_code": invitation_code,
            },
        )
        assert response.status_code == 429
        assert response.json() == {"detail": "AUTH_CAPACITY_EXCEEDED"}
    finally:
        slots.release()
        slots.release()
        with new_session() as db:
            db.execute(delete(Invitation).where(Invitation.id == invitation_id))
            db.commit()


def test_login_and_session_have_exact_shapes_and_session_ignores_trusted_identity_header():
    with _registered_account() as account:
        login = client.post(
            "/internal/v1/auth/token",
            json={"username": account["username"].upper(), "password": account["password"]},
        )
        assert login.status_code == 200, login.text
        assert set(login.json()) == {"token", "user_id", "username", "tier", "role", "expires_in"}
        assert login.headers["cache-control"] == "no-store"

        without_bearer = client.get(
            "/internal/v1/auth/session",
            headers=_trusted_header(account["user_id"]),
        )
        assert without_bearer.status_code == 401

        session = client.get(
            "/internal/v1/auth/session",
            headers=_bearer(login.json()["token"]),
        )
        assert session.status_code == 200, session.text
        assert session.json() == {
            "user_id": account["user_id"],
            "username": account["username"],
            "tier": "normal",
            "role": "user",
        }
        assert session.headers["cache-control"] == "no-store"


def test_session_rejects_jwt_missing_or_mismatching_required_claims_for_existing_user():
    with _registered_account() as account:
        valid = jwt.decode(
            account["token"], TEST_JWT_SECRET, algorithms=["HS256"], options={"verify_exp": False}
        )
        variants = []
        for missing in ("iss", "iat", "exp", "ver"):
            claims = valid.copy()
            claims.pop(missing)
            variants.append(claims)
        wrong_issuer = valid.copy()
        wrong_issuer["iss"] = "not-myink"
        variants.append(wrong_issuer)
        for claims in variants:
            token = jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256")
            response = client.get("/internal/v1/auth/session", headers=_bearer(token))
            assert response.status_code == 401
            assert response.json() == {"detail": "INVALID_CREDENTIALS"}


def test_weak_auth_secret_fails_closed_before_authentication_or_registration(monkeypatch):
    import myink.api.auth as auth

    monkeypatch.setattr(auth, "settings", replace(settings, jwt_secret="too-short"))
    username = f"weak-secret-{uuid.uuid4().hex[:8]}"
    invitation_id, invitation_code = _issue_invitation()
    register = client.post(
        "/internal/v1/auth/register",
        json={
            "username": username,
            "password": "correct horse battery 1",
            "invitation_code": invitation_code,
        },
    )
    try:
        login = client.post(
            "/internal/v1/auth/token",
            json={"username": "demo", "password": "correct horse battery 1"},
        )
        session = client.get("/internal/v1/auth/session", headers=_bearer("not-a-token"))
        for response in (register, login, session):
            assert response.status_code == 503
            assert response.json() == {"detail": "AUTH_SECRET_NOT_CONFIGURED"}
        with new_session() as db:
            assert db.scalar(select(User.id).where(User.username == username)) is None
    finally:
        if register.status_code == 201:
            with new_session() as db:
                db.execute(delete(User).where(User.username == username))
                db.execute(delete(Invitation).where(Invitation.id == invitation_id))
                db.commit()
        else:
            with new_session() as db:
                db.execute(delete(Invitation).where(Invitation.id == invitation_id))
                db.commit()


def test_logout_atomically_invalidates_old_token():
    with _registered_account() as account:
        response = client.post("/internal/v1/auth/logout", headers=_bearer(account["token"]))
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert response.headers["cache-control"] == "no-store"
        assert client.get(
            "/internal/v1/auth/session", headers=_bearer(account["token"])
        ).status_code == 401
        assert client.post(
            "/internal/v1/auth/logout", headers=_bearer(account["token"])
        ).status_code == 401


def test_password_change_atomically_revokes_token_and_requires_new_password():
    with _registered_account() as account:
        changed = client.post(
            "/internal/v1/auth/password",
            headers=_bearer(account["token"]),
            json={
                "current_password": account["password"],
                "new_password": "new-pass1",
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json() == {"ok": True}
        assert client.get(
            "/internal/v1/auth/session", headers=_bearer(account["token"])
        ).status_code == 401
        assert client.post(
            "/internal/v1/auth/token",
            json={"username": account["username"], "password": account["password"]},
        ).status_code == 401
        fresh = client.post(
            "/internal/v1/auth/token",
            json={"username": account["username"], "password": "new-pass1"},
        )
        assert fresh.status_code == 200, fresh.text


def test_password_change_rejects_wrong_current_password_without_revoking_token():
    with _registered_account() as account:
        changed = client.post(
            "/internal/v1/auth/password",
            headers=_bearer(account["token"]),
            json={"current_password": "wrong current password", "new_password": "secure12"},
        )
        assert changed.status_code == 401
        assert client.get(
            "/internal/v1/auth/session", headers=_bearer(account["token"])
        ).status_code == 200


def test_auth_schema_upgrade_is_additive_idempotent_and_preserves_legacy_books():
    import myink.db as db_module

    ensure = getattr(db_module, "ensure_user_auth_schema", None)
    ensure_role = getattr(db_module, "ensure_user_role", None)
    assert callable(ensure), "auth schema upgrader is missing"
    assert callable(ensure_role), "role schema upgrader is missing"
    with new_session() as db:
        demo = db.execute(select(User).where(User.username == "demo")).scalar_one()
        before_id = demo.id
        before_projects = set(db.scalars(select(Project.id).where(Project.user_id == demo.id)))
    ensure()
    ensure()
    ensure_role()
    ensure_role()
    columns = {column["name"] for column in inspect(get_admin_engine()).get_columns("users")}
    indexes = {index["name"] for index in inspect(get_admin_engine()).get_indexes("users")}
    assert {"password_hash", "auth_version", "role"} <= columns
    assert "uq_users_username_canonical" in indexes
    with new_session() as db:
        demo = db.get(User, before_id)
        assert demo is not None
        assert demo.password_hash is None
        assert demo.auth_version == 1
        assert demo.role == "user"
        assert set(db.scalars(select(Project.id).where(Project.user_id == before_id))) == before_projects


def test_auth_schema_upgrade_fails_without_rewriting_duplicate_normalized_legacy_names():
    import myink.db as db_module

    upgrade = getattr(db_module, "_upgrade_user_auth_schema", None)
    assert callable(upgrade), "testable auth schema transaction is missing"
    with get_admin_engine().connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("CREATE TEMP TABLE users (username VARCHAR(64) NOT NULL)"))
            conn.execute(text("INSERT INTO users (username) VALUES ('Legacy'), ('  legacy  ')"))
            with pytest.raises(RuntimeError, match="duplicate normalized usernames"):
                upgrade(conn)
            assert conn.execute(text("SELECT username FROM users ORDER BY username")).scalars().all() == [
                "  legacy  ",
                "Legacy",
            ]
        finally:
            transaction.rollback()


def test_interactive_admin_reset_preserves_books_and_revokes_existing_token():
    with _registered_account() as account:
        project_id = uuid.uuid4()
        with new_session() as db:
            db.add(Project(id=project_id, user_id=uuid.UUID(account["user_id"]), title="preserved book"))
            db.commit()
        result = runner.invoke(
            cli_app,
            ["reset-password", account["username"]],
            input="adminpass1\nadminpass1\n",
        )
        assert result.exit_code == 0, result.output
        assert client.get(
            "/internal/v1/auth/session", headers=_bearer(account["token"])
        ).status_code == 401
        assert client.post(
            "/internal/v1/auth/token",
            json={"username": account["username"], "password": account["password"]},
        ).status_code == 401
        assert client.post(
            "/internal/v1/auth/token",
            json={"username": account["username"], "password": "adminpass1"},
        ).status_code == 200
        with new_session() as db:
            assert db.get(Project, project_id) is not None


def _legacy_hash(password: str) -> str:
    """Build a persisted pre-policy scrypt fixture without the new-password validator."""
    salt = b"legacy-password!"
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=131_072, r=8, p=1,
        dklen=64, maxmem=256 * 1024 * 1024,
    )
    return "scrypt$131072$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def test_login_accepts_a_legacy_hash_that_does_not_meet_new_password_rules():
    username = f"legacy-{uuid.uuid4().hex[:10]}"
    legacy_password = "lettersonly"
    with new_session() as db:
        user = User(username=username, password_hash=_legacy_hash(legacy_password))
        db.add(user)
        db.commit()
        user_id = user.id
    try:
        response = client.post(
            "/internal/v1/auth/token",
            json={"username": username, "password": legacy_password},
        )
        assert response.status_code == 200, response.text
    finally:
        with new_session() as db:
            db.execute(delete(User).where(User.id == user_id))
            db.commit()


def test_require_admin_uses_strict_bearer_authentication_and_current_database_role():
    from myink.api.auth import require_admin

    probe = FastAPI()

    @probe.get("/admin")
    def admin_only(user=Depends(require_admin)):
        return {"username": user.username}

    probe_client = TestClient(probe)
    with _registered_account() as account:
        assert probe_client.get(
            "/admin", headers={"X-Myink-User": account["user_id"]},
        ).status_code == 401
        assert probe_client.get("/admin", headers=_bearer(account["token"])).status_code == 403

        claims = jwt.decode(
            account["token"], TEST_JWT_SECRET, algorithms=["HS256"],
            options={"verify_exp": False},
        )
        claims["role"] = "admin"
        forged_role_token = jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256")
        assert probe_client.get("/admin", headers=_bearer(forged_role_token)).status_code == 403

        with new_session() as db:
            db.execute(update(User).where(User.id == uuid.UUID(account["user_id"])).values(role="admin"))
            db.commit()
        response = probe_client.get("/admin", headers=_bearer(account["token"]))
        assert response.status_code == 200
        assert response.json() == {"username": account["username"]}


def test_set_role_changes_only_an_existing_account_and_revokes_its_sessions():
    with _registered_account() as account:
        changed = runner.invoke(cli_app, ["set-role", account["username"], "admin"])
        assert changed.exit_code == 0, changed.output
        assert client.get(
            "/internal/v1/auth/session", headers=_bearer(account["token"]),
        ).status_code == 401
        with new_session() as db:
            user = db.get(User, uuid.UUID(account["user_id"]))
            assert user is not None
            assert user.role == "admin"
            assert user.auth_version == 2

    missing = runner.invoke(cli_app, ["set-role", f"missing-{uuid.uuid4().hex[:8]}", "admin"])
    assert missing.exit_code == 1


# Existing private gateway trust boundary remains for business routes.


def test_owner_access_own_project(temp_project):
    response = client.get(
        f"/internal/v1/projects/{temp_project}/chapters",
        headers=_trusted_header(_demo_user_id()),
    )
    assert response.status_code == 200


def test_owner_rejects_foreign_project(temp_project):
    response = client.get(
        f"/internal/v1/projects/{temp_project}/chapters",
        headers=_trusted_header(uuid.uuid4()),
    )
    assert response.status_code == 403


def test_owner_fail_closed_without_identity(temp_project):
    assert client.get(f"/internal/v1/projects/{temp_project}/chapters").status_code == 403


def test_owner_rejects_invalid_identity(temp_project):
    response = client.get(
        f"/internal/v1/projects/{temp_project}/chapters",
        headers=_trusted_header("not-a-uuid"),
    )
    assert response.status_code == 403


def test_owner_missing_project_404():
    response = client.get(
        f"/internal/v1/projects/{uuid.uuid4()}/chapters",
        headers=_trusted_header(_demo_user_id()),
    )
    assert response.status_code == 404


def test_list_projects_only_own():
    with new_session() as db:
        other = User(username=f"other-{uuid.uuid4().hex[:8]}")
        db.add(other)
        db.flush()
        db.add(Project(user_id=other.id, title="other user's book"))
        db.commit()
        other_id = other.id
    try:
        response = client.get("/internal/v1/projects", headers=_trusted_header(_demo_user_id()))
        assert response.status_code == 200
        titles = {project["title"] for project in response.json()}
        assert "九州问天" in titles
        assert "other user's book" not in titles
    finally:
        with new_session() as db:
            db.execute(delete(Project).where(Project.user_id == other_id))
            db.execute(delete(User).where(User.id == other_id))
            db.commit()


def test_list_projects_fail_closed_without_identity():
    response = client.get("/internal/v1/projects")
    assert response.status_code == 200
    assert response.json() == []
