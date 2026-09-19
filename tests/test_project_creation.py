"""Draft books must not become writable before setup and outline confirmation."""

import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from myink.api.main import app
from myink.db import new_session
from myink.models import Project, User

client = TestClient(app)
OUTLINE = {
    "objective": "主角查明真相", "chapter_count": 50,
    "volumes": [{"title": "启程", "goal": "找到第一条线索", "chapter_start": 1,
                 "chapter_end": 50, "stages": [{"name": "探索", "goal": "调查",
                                                "chapter_start": 1, "chapter_end": 50}]}],
}


@pytest.fixture
def draft_book(monkeypatch):
    import myink.api.routes_book as book
    monkeypatch.setattr(book, "settings", replace(book.settings, books_per_day_max=100000))
    with new_session() as db:
        uid = db.scalar(select(User.id).where(User.username == "demo"))
    headers = {"X-Myink-User": str(uid)}
    response = client.post("/internal/v1/projects", headers=headers, json={
        "title": f"draft-test-{uuid.uuid4().hex}", "premise": "保留的创作简报",
        "chapter_count": 50, "storyline": "寻找真相",
    })
    assert response.status_code == 200, response.text
    project = response.json()
    yield project, headers
    with new_session() as db:
        db.execute(delete(Project).where(Project.id == uuid.UUID(project["id"])))
        db.commit()


def test_new_project_is_draft_and_resume_is_owned(draft_book):
    project, headers = draft_book
    assert project.get("creation_status") == "draft"
    url = f'/internal/v1/projects/{project["id"]}/creation'
    response = client.get(url, headers=headers)
    assert response.status_code == 200
    assert response.json()["context"]["premise"] == "保留的创作简报"
    assert client.get(url, headers={"X-Myink-User": str(uuid.uuid4())}).status_code == 403


def test_setup_alone_does_not_allow_writing(draft_book):
    project, headers = draft_book
    base = f'/internal/v1/projects/{project["id"]}'
    assert client.put(base + "/setup", headers=headers, json={"world_rules": {"rule": "规则"}}).status_code == 200
    assert client.get(base + "/access?write=true", headers=headers).status_code == 409
    assert client.get(base + "/creation", headers=headers).json()["project"]["creation_status"] == "setup_confirmed"


def test_outline_requires_confirmed_setup(draft_book):
    project, headers = draft_book
    base = f'/internal/v1/projects/{project["id"]}'
    assert client.put(base + "/outline", headers=headers, json=OUTLINE).status_code == 409


def test_empty_outline_cannot_promote_but_valid_confirmation_can(draft_book):
    project, headers = draft_book
    base = f'/internal/v1/projects/{project["id"]}'
    client.put(base + "/setup", headers=headers, json={"world_rules": {"rule": "规则"}})
    assert client.put(base + "/outline", headers=headers, json={}).status_code == 400
    assert client.get(base + "/access?write=true", headers=headers).status_code == 409
    assert client.put(base + "/outline", headers=headers, json=OUTLINE).status_code == 200
    assert client.get(base + "/access?write=true", headers=headers).status_code == 200
    assert client.get(base + "/creation", headers=headers).json()["project"]["creation_status"] == "ready"


def test_generated_proposals_survive_reload_without_becoming_canon(draft_book, monkeypatch):
    import myink.api.routes_book as book
    project, headers = draft_book
    base = f'/internal/v1/projects/{project["id"]}'
    monkeypatch.setattr(book, "generate_book_setup", lambda *a, **kw: ({"world_rules": {"r": "draft"}}, None))
    monkeypatch.setattr(book, "generate_book_outline", lambda *a, **kw: (OUTLINE, None))
    assert client.post(base + "/setup-draft", headers=headers, json={"premise": "简报"}).status_code == 200
    assert client.post(base + "/outline-draft", headers=headers, json={"premise": "简报", "chapter_count": 50}).status_code == 200
    response = client.get(base + "/creation", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["context"]["setup_draft"]["world_rules"] == {"r": "draft"}
    assert data["context"]["outline_draft"]["objective"] == "主角查明真相"
    assert data["project"]["creation_status"] == "draft"
    assert client.get(base + "/outline", headers=headers).json()["outline"] is None


def test_worker_also_rejects_unfinished_project(draft_book):
    from myink.worker.processor import _guard_write_order
    project, _ = draft_book
    with pytest.raises(ValueError, match="PROJECT_NOT_READY"):
        _guard_write_order(project["id"], 1)


def test_ready_project_rejects_empty_outline_update(draft_book):
    project, headers = draft_book
    base = f'/internal/v1/projects/{project["id"]}'
    client.put(base + "/setup", headers=headers, json={"world_rules": {"rule": "规则"}})
    assert client.put(base + "/outline", headers=headers, json=OUTLINE).status_code == 200
    assert client.put(base + "/outline", headers=headers, json={}).status_code == 400
    assert client.get(base + "/outline", headers=headers).json()["outline"]["objective"] == OUTLINE["objective"]


def test_creation_request_retry_returns_same_project(draft_book):
    _, headers = draft_book
    body = {"title": "idempotent draft", "request_id": str(uuid.uuid4())}
    ids = set()
    try:
        for _ in range(2):
            response = client.post("/internal/v1/projects", headers=headers, json=body)
            assert response.status_code == 200
            ids.add(uuid.UUID(response.json()["id"]))
        assert len(ids) == 1
    finally:
        with new_session() as db:
            db.execute(delete(Project).where(Project.id.in_(ids)))
            db.commit()


def test_parallel_and_late_proposals_preserve_confirmed_state(draft_book):
    from concurrent.futures import ThreadPoolExecutor
    from myink.creation import save_proposal

    project, headers = draft_book
    pid = project["id"]
    base = f"/internal/v1/projects/{pid}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(save_proposal, pid, patch) for patch in (
            {"setup_draft": {"world_rules": {"rule": "proposal"}}},
            {"outline_draft": OUTLINE},
        )]
        for result in results:
            result.result(timeout=10)
    context = client.get(base + "/creation", headers=headers).json()["context"]
    assert context["setup_draft"]["world_rules"]["rule"] == "proposal"
    assert context["outline_draft"] == OUTLINE
    client.put(base + "/setup", headers=headers, json={"world_rules": {"rule": "confirmed"}})
    save_proposal(pid, {"setup_draft": {"world_rules": {"rule": "late"}}})
    context = client.get(base + "/creation", headers=headers).json()["context"]
    assert context["setup_draft"]["world_rules"]["rule"] == "confirmed"
    client.put(base + "/outline", headers=headers, json=OUTLINE)
    context = client.get(base + "/creation", headers=headers).json()["context"]
    save_proposal(pid, {"outline_draft": {}, "setup_draft": {}})
    assert client.get(base + "/creation", headers=headers).json()["context"] == context
