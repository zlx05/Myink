"""建书向导 + 设定浏览测试（§7.11 建书流程：一句话梗概 + Planner 提案 + 用户确认落库）。

范围：
- 建书：POST /projects 创建 Project + 空 ProjectSettings（不调 LLM）；缺/非法身份 403
  fail closed；每日建书数超限（§13 bookcnt）→ 429 BOOK_CNT_EXCEEDED；空标题 400；
- 草稿：POST setup-draft stub 合法 JSON → {draft, error:None} 且不落库（DB 断言）、记
  agent_runs；stub 抛错 / 坏 JSON → {draft:{}, error} 200（§6.12 降级）；premise 空 400；
  越权矩阵（伪造他人 403 / 缺失身份 403 / 项目不存在 404 / id 非法 400）；
- 确认落库：PUT setup → world_rules/hard_constraints 整体替换 + version++、characters/
  forces/locations 按 name create-if-missing（append-only §7.11 ③，重复确认不重复建）；
  realm_cap 缺失默认「无」；
- 浏览：GET world / GET characters 读回持久化设定；空 settings 行 → 空默认不 500；越权矩阵。

模式 A（monkeypatch aiink.providers.default_provider）：make_chain 调用时读全局单例，
与 test_style_profile / test_flow stub_provider 同款。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from aiink.api.main import app
from aiink.config import settings
from aiink.db import new_session, tenant_session
from aiink.memory.repository import get_settings
from aiink.models import AgentRun, Character, Project, ProjectSettings, User
from aiink.providers.base import ModelProvider, ModelResponse

client = TestClient(app)

_SETUP_DRAFT = {
    "realm_order": ["炼气", "筑基", "金丹"],
    "world_rules": {"灵气": "天地灵气充盈", "修炼": "突破需心境圆满"},
    "hard_constraints": ["金丹期不得无敌", "凡人不可御剑飞行"],
    "forces": [{"name": "青云宗", "stance": "正道之首", "resources": ["护山大阵"]}],
    "characters": [{"name": "林砚", "role": "主角", "race": "人族", "origin": "青云镇",
                    "realm_cap": "金丹", "personality": "坚忍"}],
    "locations": [{"name": "青云山"}],
}

_SETUP_BODY = {
    "world_rules": {"灵气": "充盈"},
    "hard_constraints": ["凡人不可御剑飞行"],
    "characters": [{"name": "林砚", "realm_cap": "金丹", "race": "人族"}],
    "forces": [{"name": "青云宗", "stance": "正道之首"}],
    "locations": [{"name": "青云山"}],
}


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `aiink init`（demo 用户未建）"
        return u.id


def _h(uid: str | uuid.UUID | None) -> dict:
    """请求头：X-AiInk-User = 网关已验证的 JWT sub（None → 不带，测 fail closed）。"""
    return {"X-AiInk-User": str(uid)} if uid is not None else {}


class _BookStub(ModelProvider):
    """假 provider：建书草稿返回固定 JSON；可配坏原文 / 抛异常（测降级，§6.12）。"""

    def __init__(self, payload: dict | None = None, *, raw: str | None = None, raise_error: bool = False):
        self._payload = payload
        self._raw = raw
        self._raise_error = raise_error
        self.calls = 0

    def name(self) -> str:
        return "book-stub"

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        self.calls += 1
        if self._raise_error:
            raise RuntimeError("provider down")
        content = self._raw if self._raw is not None else json.dumps(self._payload or {}, ensure_ascii=False)
        return ModelResponse(content=content, model_id=model_id, input_tokens=10, output_tokens=20)


@pytest.fixture
def book_stub(monkeypatch):
    import aiink.providers as providers_mod

    def _install(payload: dict | None = None, *, raw: str | None = None, raise_error: bool = False) -> _BookStub:
        stub = _BookStub(payload, raw=raw, raise_error=raise_error)
        monkeypatch.setattr(providers_mod, "default_provider", stub)
        return stub

    return _install


def _book_setup_run_count(pid: str) -> int:
    """book_setup 节点的 agent_runs 行数（观测表无 RLS，new_session 可查）。"""
    with new_session() as db:
        return db.query(AgentRun).filter(
            AgentRun.project_id == uuid.UUID(pid), AgentRun.node == "book_setup").count()


def _fresh_user(db) -> uuid.UUID:
    """独立用户（当日建书计数归零，不污染 demo 用户跨测试累计）。"""
    u = User(username=f"test-book-{uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return u.id


def _delete_user(uid: uuid.UUID) -> None:
    """清用户：projects FK ondelete=CASCADE 级联清书/settings/人物（agent_runs 无级联，
    本组测试 book_setup 节点只挂 temp_project 上，由 temp_project 清理）。"""
    with new_session() as db:
        db.execute(sa_delete(User).where(User.id == uid))
        db.commit()


# ---- 建书：POST /projects ----


def test_create_project_creates_project_and_settings():
    """建书成功：Project + 空 ProjectSettings 落库，不调 LLM（响应无 draft）。"""
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        resp = client.post("/internal/v1/projects", headers=_h(uid),
                           json={"title": "破晓录", "genre": "历史悬疑"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "破晓录"
        assert data["genre"] == "历史悬疑"
        assert data["current_chapter"] == 0
        pid = uuid.UUID(data["id"])
        with tenant_session(str(pid)) as tdb:
            st = get_settings(tdb, pid)
            assert st is not None, "建书应同步建空 ProjectSettings 行"
            assert st.world_rules == {} and st.hard_constraints == []
            assert st.version == 1
        with new_session() as db:
            assert db.get(Project, pid) is not None
    finally:
        _delete_user(uid)


def test_create_project_empty_title_400():
    with new_session() as db:
        uid = _fresh_user(db)
        db.commit()
    try:
        resp = client.post("/internal/v1/projects", headers=_h(uid), json={"title": "  "})
        assert resp.status_code == 400
    finally:
        _delete_user(uid)


def test_create_project_fail_closed():
    """缺身份 / 身份非法 → 403（§14.1 ③ 默认拒绝）。"""
    body = {"title": "破晓录"}
    assert client.post("/internal/v1/projects", json=body).status_code == 403
    assert client.post("/internal/v1/projects", headers=_h("not-a-uuid"), json=body).status_code == 403


def test_create_project_daily_quota_429():
    """§13 bookcnt：当日已建 ≥ BOOKS_PER_DAY（默认 10）→ 429 BOOK_CNT_EXCEEDED。"""
    with new_session() as db:
        uid = _fresh_user(db)
        for i in range(settings.books_per_day_max):
            db.add(Project(user_id=uid, title=f"配额书{i}"))
        db.commit()
    try:
        resp = client.post("/internal/v1/projects", headers=_h(uid), json={"title": "超限书"})
        assert resp.status_code == 429
        assert resp.json() == {"error": "BOOK_CNT_EXCEEDED"}, "错误体用网关同款信封（前端 GATE_CODES 命中）"
    finally:
        _delete_user(uid)


# ---- 草稿：POST setup-draft ----


def test_setup_draft_returns_draft_not_persisted(temp_project, book_stub):
    """草稿返回合法 JSON 且不落库（DB 断言）；记 agent_runs（§6.8）。"""
    stub = book_stub(_SETUP_DRAFT)
    headers = _h(_demo_user_id())
    before = _book_setup_run_count(temp_project)
    with tenant_session(temp_project) as db:
        st_before = get_settings(db, uuid.UUID(temp_project))
    resp = client.post(
        f"/internal/v1/projects/{temp_project}/setup-draft", headers=headers,
        json={"premise": "少年从青云镇出发，闯荡仙途。"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] is None
    assert data["draft"]["realm_order"] == ["炼气", "筑基", "金丹"]
    assert data["draft"]["characters"][0]["name"] == "林砚"
    assert stub.calls == 1
    with tenant_session(temp_project) as db:
        st_after = get_settings(db, uuid.UUID(temp_project))
        if st_before is None:
            assert st_after is None, "草稿不应新建 settings 行"
        else:
            assert st_after.world_rules == st_before.world_rules, "草稿不应落库（world_rules 未变）"
            assert st_after.version == st_before.version
        assert db.query(Character).filter(
            Character.project_id == uuid.UUID(temp_project)).count() == 0, "草稿不应建角色"
    assert _book_setup_run_count(temp_project) == before + 1, "本次草稿应新增一行 agent_runs"


def test_setup_draft_degraded_on_provider_error(temp_project, book_stub):
    """LLM 抛错 → {draft:{}, error} 200（§6.12 不 500）+ 降级记 error 行。"""
    book_stub(raise_error=True)
    headers = _h(_demo_user_id())
    before = _book_setup_run_count(temp_project)
    resp = client.post(
        f"/internal/v1/projects/{temp_project}/setup-draft", headers=headers,
        json={"premise": "少年闯仙途。"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["draft"] == {}
    assert data["error"] and "provider down" in data["error"]
    assert _book_setup_run_count(temp_project) == before + 1, "降级调用也应新增 agent_runs 行"
    with new_session() as db:
        err = db.query(AgentRun).filter(
            AgentRun.project_id == uuid.UUID(temp_project),
            AgentRun.node == "book_setup", AgentRun.error.isnot(None)).first()
        assert err is not None and "provider down" in err.error


def test_setup_draft_degraded_on_bad_json(temp_project, book_stub):
    book_stub(raw="{not-json")
    resp = client.post(
        f"/internal/v1/projects/{temp_project}/setup-draft", headers=_h(_demo_user_id()),
        json={"premise": "少年闯仙途。"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["draft"] == {}
    assert data["error"] and "parse_error" in data["error"]


def test_setup_draft_empty_premise_400(temp_project):
    resp = client.post(
        f"/internal/v1/projects/{temp_project}/setup-draft", headers=_h(_demo_user_id()),
        json={"premise": "  "},
    )
    assert resp.status_code == 400


def test_setup_draft_ownership(temp_project):
    url = f"/internal/v1/projects/{temp_project}/setup-draft"
    body = {"premise": "少年闯仙途。"}
    assert client.post(url, headers=_h(uuid.uuid4()), json=body).status_code == 403   # 伪造他人
    assert client.post(url, json=body).status_code == 403                             # 缺失身份
    assert client.post(f"/internal/v1/projects/{uuid.uuid4()}/setup-draft",
                       headers=_h(_demo_user_id()), json=body).status_code == 404     # 项目不存在
    assert client.post("/internal/v1/projects/not-a-uuid/setup-draft",
                       headers=_h(_demo_user_id()), json=body).status_code == 400     # id 非法


# ---- 确认落库：PUT setup ----


def test_put_setup_persists_and_upserts_by_name(temp_project):
    """确认落库 + append-only：按 name create-if-missing，重复确认不重复建行。"""
    headers = _h(_demo_user_id())
    url = f"/internal/v1/projects/{temp_project}/setup"
    r = client.put(url, headers=headers, json=_SETUP_BODY)
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # 读回：world + characters
    w = client.get(f"/internal/v1/projects/{temp_project}/world", headers=headers)
    assert w.status_code == 200
    wd = w.json()
    assert wd["world_rules"] == {"灵气": "充盈"}
    assert wd["hard_constraints"] == ["凡人不可御剑飞行"]
    assert wd["factions"] == [{"name": "青云宗", "stance": "正道之首", "resources": []}]
    assert wd["locations"] == [{"name": "青云山"}]

    cards = client.get(f"/internal/v1/projects/{temp_project}/characters", headers=headers)
    assert cards.status_code == 200
    assert cards.json() == [{
        "id": cards.json()[0]["id"], "name": "林砚", "race": "人族", "origin": None,
        "realm_cap": "金丹", "personality": None, "base_attrs": {}, "state": {},
    }]

    # 第二次确认：同名 + 新增一个角色（realm_cap 缺省）→ 不重复建行
    r2 = client.put(url, headers=headers, json={
        **_SETUP_BODY,
        "characters": [
            {"name": "林砚", "realm_cap": "金丹"},
            {"name": "苏晚"},
        ],
        "world_rules": {"灵气": "充盈", "剑道": "剑气纵横"},
    })
    assert r2.status_code == 200
    w2 = client.get(f"/internal/v1/projects/{temp_project}/world", headers=headers)
    assert w2.json()["world_rules"] == {"灵气": "充盈", "剑道": "剑气纵横"}, "整体替换语义"
    cards2 = client.get(f"/internal/v1/projects/{temp_project}/characters", headers=headers)
    names = {c["name"] for c in cards2.json()}
    assert names == {"林砚", "苏晚"}, "林砚 不重复建、苏晚 新建"
    suwan = next(c for c in cards2.json() if c["name"] == "苏晚")
    assert suwan["realm_cap"] == "无", "realm_cap 缺失默认「无」（非空列）"
    with tenant_session(temp_project) as db:
        assert db.query(Character).filter(
            Character.project_id == uuid.UUID(temp_project)).count() == 2


def test_put_setup_increments_version(temp_project):
    headers = _h(_demo_user_id())
    url = f"/internal/v1/projects/{temp_project}/setup"
    client.put(url, headers=headers, json=_SETUP_BODY)
    with tenant_session(temp_project) as db:
        v1 = get_settings(db, uuid.UUID(temp_project)).version
    client.put(url, headers=headers, json=_SETUP_BODY)
    with tenant_session(temp_project) as db:
        v2 = get_settings(db, uuid.UUID(temp_project)).version
    assert v2 == v1 + 1, "每次确认 version 递增（§7.6 乐观版本号）"


def test_put_setup_creates_settings_when_missing(temp_project):
    """无 settings 行 → 确认落库自动新建（不 500，仿 style-profile 口径）。"""
    with tenant_session(temp_project) as db:
        db.query(ProjectSettings).filter(
            ProjectSettings.project_id == uuid.UUID(temp_project)).delete()
        db.commit()
    resp = client.put(
        f"/internal/v1/projects/{temp_project}/setup", headers=_h(_demo_user_id()),
        json={"hard_constraints": ["凡人不可御剑飞行"]},
    )
    assert resp.status_code == 200
    with tenant_session(temp_project) as db:
        st = get_settings(db, uuid.UUID(temp_project))
        assert st.hard_constraints == ["凡人不可御剑飞行"]
        assert st.version == 2, "新建行 version=1 后再 +1（对齐整体替换语义）"


def test_put_setup_ownership(temp_project):
    url = f"/internal/v1/projects/{temp_project}/setup"
    assert client.put(url, headers=_h(uuid.uuid4()), json=_SETUP_BODY).status_code == 403
    assert client.put(url, json=_SETUP_BODY).status_code == 403
    assert client.put(f"/internal/v1/projects/{uuid.uuid4()}/setup",
                      headers=_h(_demo_user_id()), json=_SETUP_BODY).status_code == 404


# ---- 浏览：GET world / GET characters ----


def test_world_empty_defaults_on_no_settings(temp_project):
    """无 settings 行 → 空默认不 500（仿 routes_settings 口径）。"""
    with tenant_session(temp_project) as db:
        db.query(ProjectSettings).filter(
            ProjectSettings.project_id == uuid.UUID(temp_project)).delete()
        db.commit()
    headers = _h(_demo_user_id())
    w = client.get(f"/internal/v1/projects/{temp_project}/world", headers=headers)
    assert w.status_code == 200
    assert w.json() == {"world_rules": {}, "hard_constraints": [], "factions": [], "locations": []}
    cards = client.get(f"/internal/v1/projects/{temp_project}/characters", headers=headers)
    assert cards.status_code == 200
    assert cards.json() == []


def test_world_characters_ownership(temp_project):
    for path in (f"/internal/v1/projects/{temp_project}/world",
                 f"/internal/v1/projects/{temp_project}/characters"):
        assert client.get(path, headers=_h(uuid.uuid4())).status_code == 403
        assert client.get(path).status_code == 403
        assert client.get(f"/internal/v1/projects/{uuid.uuid4()}/world",
                          headers=_h(_demo_user_id())).status_code == 404


# ---- 提示词契约（§7.11 AI 起名）：SYSTEM_BOOK_SETUP schema 含 title ----

def test_setup_prompt_ai_title_contract():
    """AI 起名契约：SYSTEM_BOOK_SETUP 的 JSON schema 含 title 字段（书名留空由 Planner 建议）。"""
    from aiink.workflow import prompts
    assert '"title"' in prompts.SYSTEM_BOOK_SETUP, "schema 须含书名建议字段"
    assert "已定书名返回空串" in prompts.SYSTEM_BOOK_SETUP, "已定书名时 title 返回空串（不覆盖作者决定）"
