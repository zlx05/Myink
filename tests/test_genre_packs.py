"""题材包：根目录只读、本书快照、主辅叠加、显示名锁定。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from myink.api.main import app
from myink.db import ensure_genre_pack, new_session, tenant_session

ensure_genre_pack()
from myink.genre_catalog import (
    PACKS,
    UNSELECTED_NAME,
    VOLUME_SPAN_FAST,
    VOLUME_SPAN_SLOW,
    build_book_pack,
    catalog_entries,
    compose_fields,
    display_genre,
    is_managed_pack,
    suggest_volume_count,
    volume_span_for,
)
from myink.memory.repository import get_settings
from myink.models import User

client = TestClient(app)


def _fresh_user() -> uuid.UUID:
    with new_session() as db:
        u = User(username=f"test-genre-{uuid.uuid4().hex[:8]}")
        db.add(u)
        db.flush()
        uid = u.id
        db.commit()
    return uid


def _delete_user(uid: uuid.UUID) -> None:
    with new_session() as db:
        db.execute(sa_delete(User).where(User.id == uid))
        db.commit()


def _h(uid: uuid.UUID) -> dict:
    return {"X-Myink-User": str(uid)}


def test_catalog_has_37_and_stable_ids():
    entries = catalog_entries()
    assert len(entries) == 37
    assert len(PACKS) == 37
    assert {e["id"] for e in entries} == set(PACKS)


def test_compose_secondary_overlays_only_allowed_fields():
    fields = compose_fields("xiuxian", "xitong")
    primary = compose_fields("xiuxian", None)
    assert fields["pacing"] == primary["pacing"]
    assert fields["world_hints"] == primary["world_hints"]
    assert fields["subgenres"] == primary["subgenres"]
    assert "辅题材（系统流）" in fields["selling_point"]
    assert len(fields["mechanics"]) > len(primary["mechanics"])
    assert "无铺垫越级突破" in fields["taboos"]
    assert "面板每章完整刷屏" in fields["taboos"]


def test_volume_span_follows_pacing():
    assert volume_span_for(build_book_pack("xitong", None)) == VOLUME_SPAN_FAST
    assert volume_span_for(build_book_pack("xiuxian", None)) == VOLUME_SPAN_SLOW
    assert suggest_volume_count(200, build_book_pack("xitong", None)) > suggest_volume_count(
        200, build_book_pack("xiuxian", None))
    assert 3 <= suggest_volume_count(80, None) <= 20


def test_display_genre_locked_names():
    assert display_genre(None, None) == UNSELECTED_NAME
    assert display_genre("xiuxian", None) == "修仙"
    assert display_genre("xiuxian", "xitong") == "修仙+系统流"


def test_book_pack_baseline_isolated_from_later_edits():
    pack = build_book_pack("xiuxian", None)
    assert is_managed_pack(pack)
    original = pack["selling_point"]
    pack["selling_point"] = "用户改过"
    assert pack["baseline"]["selling_point"] == original


def test_create_without_primary_id_keeps_old_genre_string():
    uid = _fresh_user()
    try:
        resp = client.post("/internal/v1/projects", headers=_h(uid),
                           json={"title": "旧接口书", "genre": "历史悬疑"})
        assert resp.status_code == 200
        assert resp.json()["genre"] == "历史悬疑"
        pid = resp.json()["id"]
        with tenant_session(pid) as db:
            st = get_settings(db, uuid.UUID(pid))
            assert st is not None
            assert st.genre_pack == {} or st.genre_pack is None or st.genre_pack == {}
    finally:
        _delete_user(uid)


def test_create_with_pack_locks_name_and_stores_snapshot():
    uid = _fresh_user()
    try:
        resp = client.post("/internal/v1/projects", headers=_h(uid), json={
            "title": "新包书",
            "primary_id": "xiuxian",
            "secondary_id": "xitong",
            "genre_fields": {"pacing": "用户自己的节奏"},
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["genre"] == "修仙+系统流"
        pid = resp.json()["id"]
        with tenant_session(pid) as db:
            st = get_settings(db, uuid.UUID(pid))
            assert is_managed_pack(st.genre_pack)
            assert st.genre_pack["source_id"] == "xiuxian"
            assert st.genre_pack["secondary_id"] == "xitong"
            assert st.genre_pack["pacing"] == "用户自己的节奏"
            assert st.genre_pack["baseline"]["pacing"] == "用户自己的节奏"
        listed = client.get("/internal/v1/genre-packs", headers=_h(uid))
        assert listed.status_code == 200
        assert len(listed.json()) == 37
        settings = client.get(f"/internal/v1/projects/{pid}/settings", headers=_h(uid))
        assert settings.status_code == 200
        pub = settings.json()["genre_pack"]
        assert "baseline" not in pub
        assert pub["source_name"] == "修仙"
        locked = client.put(f"/internal/v1/projects/{pid}", headers=_h(uid),
                            json={"genre": "都市"})
        assert locked.status_code == 400
        saved = client.put(f"/internal/v1/projects/{pid}/genre-pack", headers=_h(uid),
                           json={"taboos": ["本书自己的禁忌"]})
        assert saved.status_code == 200
        assert saved.json()["taboos"] == ["本书自己的禁忌"]
        assert saved.json()["pacing"] == "用户自己的节奏"
        restored = client.post(f"/internal/v1/projects/{pid}/genre-pack/restore",
                               headers=_h(uid))
        assert restored.status_code == 200
        assert restored.json()["pacing"] == "用户自己的节奏"
        assert "本书自己的禁忌" not in restored.json()["taboos"]
    finally:
        _delete_user(uid)


def test_secondary_requires_primary_400():
    uid = _fresh_user()
    try:
        resp = client.post("/internal/v1/projects", headers=_h(uid), json={
            "title": "只有辅",
            "primary_id": None,
            "secondary_id": "xitong",
        })
        assert resp.status_code == 400
    finally:
        _delete_user(uid)


def test_old_book_cannot_put_genre_pack():
    uid = _fresh_user()
    try:
        resp = client.post("/internal/v1/projects", headers=_h(uid),
                           json={"title": "旧书"})
        pid = resp.json()["id"]
        put = client.put(f"/internal/v1/projects/{pid}/genre-pack", headers=_h(uid),
                         json={"pacing": "想补"})
        assert put.status_code == 400
    finally:
        _delete_user(uid)
