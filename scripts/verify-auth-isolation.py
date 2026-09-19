"""Live gateway/API smoke check; writes only new fixtures to the dedicated test DB.

Run with the test environment from docs/DEPLOY.md and services bound to ports
18080 (gateway), 18100 (API). No cleanup/delete or real model calls are performed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.engine import make_url

from myink.config import settings
from myink.db import new_session, tenant_session
from myink.models import Chapter, Task
from myink.invitations import create_invitation
from myink.worker.redis_client import get_redis


def main() -> None:
    database = make_url(settings.database_url)
    if settings.app_env != "test" or database.host != "127.0.0.1" or database.port != 15432:
        raise RuntimeError("Refusing to write fixtures outside the dedicated local test database")
    redis = urlparse(settings.redis_url)
    if (redis.scheme != "redis" or redis.hostname != "127.0.0.1"
            or redis.port != 16380 or redis.path != "/0" or redis.query):
        raise RuntimeError("Refusing to write fixtures outside the dedicated local test Redis")

    checks = 0
    with httpx.Client(base_url="http://127.0.0.1:18080/api/v1", timeout=20) as client:
        def request(method, path, status, token=None, **kwargs):
            nonlocal checks
            headers = kwargs.pop("headers", {})
            if token:
                headers["Authorization"] = f"Bearer {token}"
            response = client.request(method, path, headers=headers, **kwargs)
            assert response.status_code == status, (method, path, response.status_code)
            checks += 1
            return response

        password = "isolated test password 2026"
        with new_session() as db:
            invites = [create_invitation(db, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))[1]
                       for _ in range(2)]
            db.commit()
        request("POST", "/auth/register", 403, json={
            "username": "no-invite-" + uuid.uuid4().hex, "password": password,
        })
        users = [request("POST", "/auth/register", 201, json={
            "username": "smoke-" + uuid.uuid4().hex, "password": password,
            "invitation_code": invitation,
        }).json() for invitation in invites]
        first, second = users
        for user in users:
            request("GET", "/auth/session", 200, user["token"])
        request("POST", "/auth/token", 401, json={"username": first["username"], "password": "incorrect password"})
        request("POST", "/auth/token", 422, json={"username": first["username"]})

        books = [request("POST", "/projects", 200, user["token"], json={
            "title": "private-smoke-" + user["user_id"],
        }).json() for user in users]
        own, foreign = books[0]["id"], books[1]["id"]
        for book, user in zip(books, users):
            base = f'/projects/{book["id"]}'
            request("POST", base + "/chapters/new/generate", 409, user["token"], json={"seq": 1})
            request("PUT", base + "/setup", 200, user["token"], json={"world_rules": {"rule": "test fixture"}})
            request("PUT", base + "/outline", 200, user["token"], json={
                "objective": "test fixture", "chapter_count": 50,
                "volumes": [{"title": "first", "goal": "test goal", "chapter_start": 1, "chapter_end": 50}],
            })
        for index, user in enumerate(users):
            visible = request("GET", "/projects", 200, user["token"]).json()
            assert [book["id"] for book in visible] == [books[index]["id"]]

        with tenant_session(uuid.UUID(own)) as db:
            chapter = Chapter(project_id=uuid.UUID(own), chapter_seq=1,
                              title="owner only", content="private writing smoke fixture")
            db.add(chapter)
            db.commit()
            chapter_id = str(chapter.id)
        with new_session() as db:
            task = Task(project_id=uuid.UUID(own), task_type="chapter_generate",
                        status="paused", payload={"seq": 1, "secret": "private plan"})
            db.add(task)
            db.commit()
            task_id = str(task.id)
        get_redis().xadd(f"queue:sse:{task_id}", {"event": "status", "status": "done"})
        get_redis().expire(f"queue:sse:{task_id}", 3600)

        request("GET", f"/projects/{own}/chapters/{chapter_id}", 200, first["token"])
        request("GET", f"/tasks/{task_id}", 200, first["token"])
        stream = request("GET", f"/tasks/{task_id}/events", 200, first["token"])
        assert stream.headers["cache-control"] == "no-store"
        for method, path, payload in (
            ("GET", f"/projects/{own}/chapters/{chapter_id}", None),
            ("GET", f"/tasks/{task_id}", None),
            ("GET", f"/tasks/{task_id}/events", None),
            ("POST", f"/batches/{task_id}/pause", None),
            ("POST", f"/batches/{task_id}/resume", None),
            ("POST", f"/tasks/{task_id}/cancel", None),
            ("POST", f"/tasks/{task_id}/plan/confirm", {"plan": {}, "expected_attempt": 1}),
            ("POST", f"/projects/{own}/chapters/new/generate", {"seq": 2}),
            ("POST", f"/projects/{own}/batches/generate", {"n": 2, "start_seq": 2}),
        ):
            denied = request(method, path, 403, second["token"], json=payload,
                             headers={"X-Myink-User": first["user_id"]})
            assert "private plan" not in denied.text and "private writing" not in denied.text
        with new_session() as db:
            assert db.get(Task, uuid.UUID(task_id)).status == "paused"

        queued = request("POST", f"/projects/{foreign}/chapters/new/generate", 202,
                         second["token"], json={"seq": 1}).json()
        request("GET", f"/tasks/{queued['task_id']}/events", 403, first["token"])

        new_password = "replacement test password 2026"
        request("POST", "/auth/password", 200, first["token"], json={
            "current_password": password, "new_password": new_password,
        })
        request("GET", "/projects", 401, first["token"])
        request("POST", "/auth/token", 401, json={"username": first["username"], "password": password})
        renewed = request("POST", "/auth/token", 200, json={
            "username": first["username"], "password": new_password,
        }).json()
        request("GET", "/projects", 200, renewed["token"])
        request("POST", "/auth/logout", 200, renewed["token"])
        request("GET", "/projects", 401, renewed["token"])
        request("GET", "/projects", 200, second["token"])

    print(f"PASS: {checks} live HTTP checks; two registered users, books, writing tasks, SSE, password change and logout")
    print("Fixtures remain only in the dedicated disposable test services; no data was deleted.")


if __name__ == "__main__":
    main()
