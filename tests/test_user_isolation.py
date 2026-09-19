"""Cross-account requests must not read or mutate writing tasks or tenant data."""

import uuid

import pytest
from fastapi.testclient import TestClient

from myink.api.main import app
from myink.db import new_session, tenant_session
from myink.models import AgentRun, Chapter, Project, Task, User

client = TestClient(app)


@pytest.fixture
def accounts_and_books():
    with new_session() as db:
        users = [User(username=f"iso-{uuid.uuid4().hex}") for _ in range(2)]
        db.add_all(users)
        db.flush()
        books = [Project(user_id=u.id, title=f"private-{u.id}") for u in users]
        db.add_all(books)
        db.commit()
    try:
        yield users, books
    finally:
        with new_session() as db:
            db.query(AgentRun).filter(AgentRun.project_id.in_([b.id for b in books])).delete()
            db.query(User).filter(User.id.in_([u.id for u in users])).delete()
            db.commit()


def headers(user):
    return {"X-Myink-User": str(user.id)}


@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None),
    ("POST", "/pause", None),
    ("POST", "/resume", None),
    ("POST", "/cancel", None),
    ("POST", "/plan/confirm", {"plan": {}, "expected_attempt": 1}),
])
@pytest.mark.parametrize("identity", ["foreign", "missing"])
def test_task_endpoints_reject_non_owner(accounts_and_books, method, suffix, body, identity, monkeypatch):
    users, books = accounts_and_books
    with new_session() as db:
        task = Task(project_id=books[0].id, task_type="chapter_generate", status="paused",
                    payload={"seq": 1, "secret": "owner-only-outline"})
        db.add(task)
        db.commit()
    published = []
    monkeypatch.setattr("myink.api.routes_tasks.amqp.publish", lambda *a, **kw: published.append(a))
    response = client.request(method, f"/internal/v1/tasks/{task.id}{suffix}",
                              json=body, headers=headers(users[1]) if identity == "foreign" else {})
    assert response.status_code in (401, 403, 404)
    assert "owner-only-outline" not in response.text
    assert published == []
    with new_session() as db:
        assert db.get(Task, task.id).status == "paused"


def test_owner_can_read_own_task(accounts_and_books):
    users, books = accounts_and_books
    with new_session() as db:
        task = Task(project_id=books[0].id, task_type="chapter_generate", payload={"seq": 1})
        db.add(task)
        db.commit()
    response = client.get(f"/internal/v1/tasks/{task.id}", headers=headers(users[0]))
    assert response.status_code == 200
    assert response.json()["task_id"] == str(task.id)


def test_pending_task_access_uses_gateway_owner_record(accounts_and_books):
    import json
    from myink.worker.redis_client import get_redis

    users, books = accounts_and_books
    task_id = str(uuid.uuid4())
    key = f"queue:task-owner:{task_id}"
    redis = get_redis()
    redis.set(key, json.dumps({"user_id": str(users[0].id), "project_id": str(books[0].id)}), ex=60)
    try:
        path = f"/internal/v1/tasks/{task_id}/access"
        assert client.get(path, headers=headers(users[0])).status_code == 200
        assert client.get(path, headers=headers(users[1])).status_code == 403
        assert client.get(path).status_code == 403
    finally:
        redis.delete(key)


def test_project_access_checks_owner(accounts_and_books):
    users, books = accounts_and_books
    path = f"/internal/v1/projects/{books[0].id}/access"
    assert client.get(path, headers=headers(users[0])).status_code == 200
    assert client.get(path, headers=headers(users[1])).status_code == 403


@pytest.mark.parametrize("suffix", ["chapters", "world", "characters", "settings", "tasks",
                                     "candidates", "lessons", "events", "graph", "outline"])
def test_book_views_reject_other_account(accounts_and_books, suffix):
    users, books = accounts_and_books
    response = client.get(f"/internal/v1/projects/{books[0].id}/{suffix}", headers=headers(users[1]))
    assert response.status_code in (403, 404)


def test_rls_and_root_lists_do_not_cross_accounts(accounts_and_books):
    users, books = accounts_and_books
    chapter_ids = []
    for book in books:
        with tenant_session(book.id) as db:
            chapter = Chapter(project_id=book.id, chapter_seq=1, content=f"secret-{book.id}", status="confirmed")
            db.add(chapter)
            db.flush()
            chapter_ids.append(chapter.id)
    for index, user in enumerate(users):
        response = client.get("/internal/v1/projects", headers=headers(user))
        assert [b["id"] for b in response.json()] == [str(books[index].id)]
        with tenant_session(books[index].id) as db:
            assert db.get(Chapter, chapter_ids[1-index]) is None
            assert db.get(Chapter, chapter_ids[index]).content == f"secret-{books[index].id}"
    with new_session() as db:
        assert db.query(Chapter).filter(Chapter.id.in_(chapter_ids)).all() == []


@pytest.mark.parametrize("forgery", ["foreign_user", "missing_user", "task_project"])
def test_worker_rejects_forged_ownership_before_dispatch(accounts_and_books, forgery, monkeypatch):
    from myink.worker import processor
    users, books = accounts_and_books
    task_id = uuid.uuid4()
    if forgery == "task_project":
        with new_session() as db:
            db.add(Task(id=task_id, project_id=books[1].id, task_type="chapter_generate", status="queued"))
            db.commit()
    calls = []
    monkeypatch.setattr(processor, "_dispatch", lambda body: calls.append(body) or {})
    body = {"task_id": str(task_id), "project_id": str(books[0].id), "task_type": "chapter_generate",
            "user_id": str(users[1 if forgery == "foreign_user" else 0].id), "payload": {"seq": 1}}
    if forgery == "missing_user":
        body.pop("user_id")
    assert processor.process(body) == "skip"
    assert calls == []
    with new_session() as db:
        task = db.get(Task, task_id)
        if forgery == "task_project":
            assert task.project_id == books[1].id and task.status == "queued"
        else:
            assert task is None


@pytest.mark.parametrize("failure_point", ["initial", "process"])
def test_consumer_requeues_transient_ownership_database_failure(accounts_and_books, monkeypatch, failure_point):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    from psycopg import OperationalError
    from myink.worker import consumer, processor

    users, books = accounts_and_books
    body = {"task_id": str(uuid.uuid4()), "project_id": str(books[0].id),
            "user_id": str(users[0].id), "task_type": "chapter_generate", "payload": {"seq": 1}}
    def disconnected(_body):
        raise OperationalError("temporary database disconnect")
    monkeypatch.setattr(consumer, "observe", lambda *args: None)
    if failure_point == "initial":
        monkeypatch.setattr(consumer, "valid_task_owner", disconnected)
    else:
        monkeypatch.setattr(processor, "valid_task_owner", disconnected)
    channel, redis = Mock(), Mock()
    consumer._on_message(channel, SimpleNamespace(delivery_tag=7), None,
                         json.dumps(body).encode(), redis, "isolation-test")
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(7, requeue=True)


def test_consumer_rejects_foreign_message_without_publishing_events(accounts_and_books):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    from myink.worker import consumer

    users, books = accounts_and_books
    body = {"task_id": str(uuid.uuid4()), "project_id": str(books[0].id),
            "user_id": str(users[1].id), "task_type": "chapter_generate", "payload": {"seq": 1}}
    channel, redis = Mock(), Mock()
    consumer._on_message(channel, SimpleNamespace(delivery_tag=7), None,
                         json.dumps(body).encode(), redis, "isolation-test")
    channel.basic_ack.assert_called_once_with(7)
    redis.xadd.assert_not_called()
