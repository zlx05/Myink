"""作品信息更新测试（§6.9 每章目标字数可配）。

范围：
- 建书带 target_words：POST /projects 收 target_words（500–20000），越界 400；
- 更新：PUT /projects/{pid} 非 None 字段才更新（title/genre/target_words），target_words
  显式传 null → 置空（清空回落生成侧默认 3000）；越界 400；
- 越权矩阵：伪造他人 403 / 缺失身份 403 / 不存在 404 / id 非法 400。

模式：独立用户（当日建书计数不污染 demo），同 test_book_setup。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from myink.api.main import app
from myink.db import new_session
from myink.models import Project, User

client = TestClient(app)


def _fresh_user(db) -> uuid.UUID:
    u = User(username=f"test-proj-{uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return u.id


def _delete_user(uid: uuid.UUID) -> None:
    with new_session() as db:
        db.execute(sa_delete(User).where(User.id == uid))
        db.commit()


def _h(uid: str | uuid.UUID | None) -> dict:
    return {"X-Myink-User": str(uid)} if uid is not None else {}


def _make_book(uid) -> dict:
    resp = client.post("/internal/v1/projects", headers=_h(uid),
                       json={"title": "字数书", "genre": "仙侠玄幻", "target_words": 2500})
    assert resp.status_code == 200
    return resp.json()


def test_create_project_with_target_words():
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        data = _make_book(uid)
        assert data["target_words"] == 2500
        with new_session() as db:
            assert db.get(Project, uuid.UUID(data["id"])).target_words == 2500
    finally:
        _delete_user(uid)


def test_list_projects_includes_target_words():
    """项目列表回显 target_words（前端设置页从 listProjects 读该书当前字数）。"""
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        data = _make_book(uid)
        resp = client.get("/internal/v1/projects", headers=_h(uid))
        assert resp.status_code == 200
        mine = [p for p in resp.json() if p["id"] == data["id"]]
        assert mine and mine[0]["target_words"] == 2500
    finally:
        _delete_user(uid)


def test_create_project_invalid_target_words_400():
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        for bad in (0, 100, 99999):
            resp = client.post("/internal/v1/projects", headers=_h(uid),
                               json={"title": "坏字数", "target_words": bad})
            assert resp.status_code == 400, f"target_words={bad} 应 400"
    finally:
        _delete_user(uid)


def test_update_project_target_words():
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        data = _make_book(uid)
        pid = data["id"]
        resp = client.put(f"/internal/v1/projects/{pid}", headers=_h(uid),
                          json={"target_words": 3500})
        assert resp.status_code == 200
        assert resp.json()["target_words"] == 3500
        assert resp.json()["title"] == "字数书"  # 未传字段不覆盖
        with new_session() as db:
            assert db.get(Project, uuid.UUID(pid)).target_words == 3500
    finally:
        _delete_user(uid)


def test_update_project_clear_target_words():
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        data = _make_book(uid)
        resp = client.put(f"/internal/v1/projects/{data['id']}", headers=_h(uid),
                          json={"target_words": None})
        assert resp.status_code == 200
        assert resp.json()["target_words"] is None
    finally:
        _delete_user(uid)


def test_update_project_out_of_range_400():
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        data = _make_book(uid)
        resp = client.put(f"/internal/v1/projects/{data['id']}", headers=_h(uid),
                          json={"target_words": 300})
        assert resp.status_code == 400
    finally:
        _delete_user(uid)


def test_update_project_owner_matrix():
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        data = _make_book(uid)
        pid = data["id"]
        body = {"target_words": 3000}
        assert client.put(f"/internal/v1/projects/{pid}", json=body).status_code == 403     # 缺失身份
        assert client.put(f"/internal/v1/projects/{pid}", headers=_h(uuid.uuid4()),
                          json=body).status_code == 403                                     # 伪造他人
        assert client.put(f"/internal/v1/projects/{uuid.uuid4()}", headers=_h(uid),
                          json=body).status_code == 404                                     # 不存在
        assert client.put("/internal/v1/projects/not-a-uuid", headers=_h(uid),
                          json=body).status_code == 400                                     # id 非法
    finally:
        _delete_user(uid)
