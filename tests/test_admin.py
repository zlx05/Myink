"""Admin reads cross tenants only after a fresh bearer role check."""
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from myink.api import auth
from myink.api.main import app
from myink.config import settings
from myink.db import get_admin_engine, new_session, tenant_session
from myink.models import AgentRun, Chapter, Fact, Invitation, Project, ProjectSettings, Task, User

client = TestClient(app, raise_server_exceptions=False)
PREFIX = "/internal/v1/admin"


@pytest.fixture
def admin_data(monkeypatch):
    monkeypatch.setattr(auth, "settings", replace(settings, jwt_secret="admin-test-secret-at-least-32-characters"))
    with new_session() as db:
        users = [User(username=f"adm-test-{uuid.uuid4().hex}", role=role,
                      environment={"api_key": "ENV-DO-NOT-EXPOSE"}) for role in ("admin", "user")]
        db.add_all(users)
        db.flush()
        books = [Project(user_id=u.id, title=f"book-{u.id}") for u in users]
        db.add_all(books)
        db.flush()
        task = Task(project_id=books[1].id, task_type="batch_generate", status="paused",
                    payload={"password": "PAYLOAD-SECRET", "size": 2}, error="Bearer ERROR-SECRET")
        db.add(task)
        db.flush()
        runs = [AgentRun(project_id=pid, task_id=tid, node="write", input_tokens=n,
                         output_tokens=2*n, cost_est=n/10, duration_ms=n*100,
                         detail={"api_key": "RUN-SECRET", "output": "story"})
                for pid, tid, n in [(books[1].id, str(task.id), 3),
                                    (books[1].id, f"{task.id}:ch1", 5),
                                    (books[1].id, f"{task.id}-wrong", 100),
                                    (books[0].id, str(task.id), 1000),
                                    (books[1].id, None, 7)]]
        db.add_all(runs)
        db.commit()
    chapters = []
    for book in books:
        with tenant_session(book.id) as db:
            chapter = Chapter(project_id=book.id, chapter_seq=1, content="正文abc", summary="summary")
            db.add(chapter)
            db.add(ProjectSettings(project_id=book.id, world_rules={"setting": "moon", "secret": "WORLD-SECRET"},
                                   model_routes={"api_key": "ROUTE-SECRET"}))
            db.flush()
            chapters.append(chapter)
    try:
        yield users, books, task, runs, chapters
    finally:
        with new_session() as db:
            db.query(AgentRun).filter(AgentRun.project_id.in_([b.id for b in books])).delete()
            # Audit retains actors after account deletion, like production.
            db.query(User).filter(User.id.in_([u.id for u in users])).delete()
            db.commit()


def bearer(user):
    return {"Authorization": "Bearer " + auth.create_access_token(user.id, auth_version=user.auth_version)}


@pytest.mark.parametrize("identity,status", [("missing",401), ("forged",401), ("user",403), ("admin",200)])
def test_admin_authority_and_no_store(admin_data, identity, status):
    users, *_ = admin_data
    headers = {} if identity == "missing" else {"X-Myink-User": str(users[0].id)} if identity == "forged" else bearer(users[identity == "user"])
    response = client.get(PREFIX + "/overview", headers=headers)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"


def test_admin_reads_cross_user_paginated_metadata_and_exact_batch_sums(admin_data):
    users, books, task, runs, chapters = admin_data
    headers = bearer(users[0])
    response = client.get(PREFIX+f"/tasks?user_id={users[1].id}&limit=1", headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["limit"], page["offset"]) == (1, 1, 0)
    row = page["items"][0]
    assert row["metrics"] == {"run_count": 2, "input_tokens": 8, "output_tokens":16, "cost_est":0.8, "duration_ms":800}
    assert "payload" not in row
    detail = client.get(PREFIX+f"/tasks/{task.id}", headers=headers).json()
    assert detail["payload"]["data"]["size"] == 2
    assert detail["elapsed_includes_waits"] is True
    assert "PAYLOAD-SECRET" not in str(detail) and "ERROR-SECRET" not in str(detail)
    run_page = client.get(PREFIX+f"/tasks/{task.id}/runs?limit=1&offset=1", headers=headers).json()
    assert run_page["total"] == 2 and len(run_page["items"]) == 1
    assert "detail" not in run_page["items"][0]
    all_runs = client.get(PREFIX+f"/runs?project_id={books[1].id}", headers=headers).json()
    assert all_runs["total"] == 4 and any(r["task_id"] is None for r in all_runs["items"])
    projects = client.get(PREFIX+f"/projects?user_id={users[1].id}", headers=headers).json()
    assert projects["total"] == 1 and projects["items"][0]["word_count"] == 5
    chapter_page = client.get(PREFIX+f"/projects/{books[1].id}/chapters", headers=headers).json()
    assert chapter_page["total"] == 1 and "content" not in chapter_page["items"][0]
    chapter = client.get(PREFIX+f"/projects/{books[1].id}/chapters/{chapters[1].id}", headers=headers)
    assert chapter.json()["content"] == "正文abc"
    wrong = client.get(PREFIX+f"/projects/{books[0].id}/chapters/{chapters[1].id}", headers=headers)
    assert wrong.status_code == 404 and wrong.headers["cache-control"] == "no-store"
    context = client.get(PREFIX+f"/projects/{books[1].id}/context", headers=headers)
    assert context.status_code == 200 and "moon" in context.text
    assert all(s not in context.text for s in ("WORLD-SECRET", "ROUTE-SECRET", "model_routes", "environment"))
    run_detail = client.get(PREFIX+f"/runs/{runs[0].id}", headers=headers).json()
    assert "RUN-SECRET" not in str(run_detail) and run_detail["prompt_missing"] is True
    safe_users = client.get(PREFIX+f"/users?q={users[1].username}", headers=headers).json()
    assert safe_users["total"] == 1 and "ENV-DO-NOT-EXPOSE" not in str(safe_users)
    logs = client.get(PREFIX+"/access-logs?limit=100", headers=headers).json()
    assert any(r["actor_id"] == str(users[0].id) and r["action"] == "admin.task" for r in logs["items"])


def test_admin_revocation_and_validation_errors(admin_data):
    users, *_ = admin_data
    headers = bearer(users[0])
    for query in ("limit=101", "offset=-1"):
        response = client.get(PREFIX+"/users?"+query, headers=headers)
        assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    with new_session() as db:
        db.get(User, users[0].id).role = "user"
        db.commit()
    assert client.get(PREFIX+"/overview", headers=headers).status_code == 403
    with new_session() as db:
        user = db.get(User, users[0].id)
        user.role = "admin"
        user.auth_version += 1
        db.commit()
    assert client.get(PREFIX+"/overview", headers=headers).status_code == 401


def test_admin_read_connection_rejects_writes_and_audit_fails_closed(admin_data, monkeypatch):
    from myink.api import routes_admin
    with routes_admin.admin_read_session() as db:
        assert db.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        with pytest.raises(Exception, match="read-only"):
            db.execute(text("UPDATE users SET tier = tier WHERE false"))
    def unavailable(*args, **kwargs):
        raise RuntimeError("Bearer DO-NOT-EXPOSE")
    monkeypatch.setattr(routes_admin, "write_access_log", unavailable)
    response = client.get(PREFIX+"/overview", headers=bearer(admin_data[0][0]))
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert "DO-NOT-EXPOSE" not in response.text


def test_context_limits_legacy_missing_and_owner_aggregates(admin_data):
    users, books, task, runs, _ = admin_data
    headers = bearer(users[0])
    with tenant_session(books[1].id) as db:
        db.add_all([Fact(project_id=books[1].id, content=f"fact-{i}", source_chapter=1) for i in range(3)])
    with new_session() as db:
        db.get(AgentRun, runs[4].id).detail = None
        db.commit()
    data = client.get(PREFIX+f"/projects/{books[1].id}/context?limit=2", headers=headers).json()
    assert data["facts"]["total"] == 3 and len(data["facts"]["items"]) == 2 and data["facts"]["truncated"]
    row = client.get(PREFIX+f"/users?q={users[1].username}", headers=headers).json()["items"][0]
    assert (row["project_count"], row["chapter_count"], row["word_count"], row["task_count"]) == (1, 1, 5, 1)
    assert row["metrics"]["run_count"] == 4 and row["metrics"]["input_tokens"] == 115
    legacy = client.get(PREFIX+f"/runs/{runs[4].id}", headers=headers).json()
    assert legacy["detail_missing"] and legacy["prompt_missing"] and legacy["detail"]["data"] is None
    filtered = client.get(PREFIX+f"/tasks?user_id={users[1].id}&status=failed", headers=headers).json()
    assert filtered["total"] == 0 and filtered["items"] == []
    empty = client.get(PREFIX+f"/projects?user_id={users[1].id}&offset=1", headers=headers).json()
    assert empty["total"] == 1 and empty["items"] == []


def test_actual_registration_and_login_accept_exactly_eight_characters(admin_data):
    from myink.invitations import create_invitation
    username = f"eight-{uuid.uuid4().hex}"
    with new_session() as db:
        invitation, code = create_invitation(db, expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
        db.commit()
    try:
        response = client.post("/internal/v1/auth/register", json={"username":username, "password":"abcd1234", "invitation_code":code})
        assert response.status_code == 201, response.text
        login = client.post("/internal/v1/auth/token", json={"username":username, "password":"abcd1234"})
        assert login.status_code == 200, login.text
        assert login.json()["role"] == "user"
    finally:
        with new_session() as db:
            db.query(User).filter(User.username == username).delete()
            db.query(Invitation).filter(Invitation.id == invitation.id).delete()
            db.commit()


def test_admin_run_read_bounds_and_scrubs_lossless_business_detail(admin_data):
    import json
    from myink.admin_observability import capture_detail
    users, _, _, runs, _ = admin_data
    original = {"password": "LEGACY-CONTROL-SECRET", "goals": ["scene " * 500] * 100}
    stored = capture_detail({"plan": original, "plan_attempt": 7, "writing_mode": "manual",
                             "messages": [{"role": "user", "content": "prompt " * 5000}] * 100})
    assert stored["plan"] == original
    with new_session() as db:
        db.get(AgentRun, runs[0].id).detail = stored
        db.commit()
    response = client.get(PREFIX+f"/runs/{runs[0].id}", headers=bearer(users[0]))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["detail"]
    assert data["truncated"] and "LEGACY-CONTROL-SECRET" not in response.text
    assert len(json.dumps(data).encode()) <= 65536
