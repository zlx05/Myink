"""账号级环境配置：模型连接/路由 + MCP 扫榜（不绑具体作品）。"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete as sa_delete

from myink.api import routes_environment
from myink.api.routes_environment import (
    EnvironmentBody,
    ModelConnectionBody,
    get_environment,
    put_environment,
)
from myink.api.routes_settings import SettingsBody
from myink.api.routes_settings import put_project_settings
from myink.db import ensure_user_environment, new_session
from myink.environment import save_raw
from myink.models import User
from myink.providers import make_chain
from myink.providers.connections import CONNECTIONS_KEY
from myink.providers.credentials import decrypt_api_key


ensure_user_environment()


@pytest.fixture
def temp_user():
    with new_session() as db:
        user = User(username=f"env-{uuid.uuid4().hex[:8]}")
        db.add(user)
        db.commit()
        uid = str(user.id)
    yield uid
    with new_session() as db:
        db.execute(sa_delete(User).where(User.id == uuid.UUID(uid)))
        db.commit()


def test_environment_roundtrip_models_and_rankings(temp_user):
    empty = get_environment(user_id=temp_user)
    assert empty["model_routes"] == {}
    assert empty["model_connections"] == []
    assert empty["thinking_enabled"] is False
    assert empty["rankings"]["mcp_url"]

    cid = str(uuid.uuid4())
    out = put_environment(EnvironmentBody(
        model_connections=[ModelConnectionBody(
            id=cid, name="私有 OpenAI", protocol="openai",
            base_url="https://models.example.com/v1/", model="novel-pro",
            api_key="secret-value",
        )],
        model_routes={"writer": f"custom:{cid}"},
        rankings={"enabled": False, "mcp_url": "https://mcp.example.com/api",
                  "timeout": 8, "limit": 5, "source": "qidian", "tool": "rank_tool"},
    ), user_id=temp_user)

    assert out["model_routes"] == {"writer": f"custom:{cid}"}
    assert out["model_connections"] == [{
        "id": cid, "name": "私有 OpenAI", "protocol": "openai",
        "base_url": "https://models.example.com/v1", "model": "novel-pro",
        "input_price": None, "output_price": None, "has_api_key": True,
    }]
    assert out["rankings"]["enabled"] is False
    assert out["rankings"]["mcp_url"] == "https://mcp.example.com/api"
    assert out["rankings"]["timeout"] == 8
    assert "secret-value" not in repr(out)

    with new_session() as db:
        stored = db.get(User, uuid.UUID(temp_user)).environment
        encrypted = stored["models"][CONNECTIONS_KEY][cid]["api_key_encrypted"]
    assert decrypt_api_key(encrypted) == "secret-value"


def test_environment_connection_prices_roundtrip_and_chain(temp_user, temp_project):
    cid = str(uuid.uuid4())
    out = put_environment(EnvironmentBody(
        model_connections=[ModelConnectionBody(
            id=cid, name="私有", protocol="openai",
            base_url="https://models.example.com/v1", model="novel-pro",
            api_key="k", input_price=3.5, output_price=8,
        )],
        model_routes={"writer": f"custom:{cid}"},
    ), user_id=temp_user)
    assert out["model_connections"][0]["input_price"] == 3.5
    assert out["model_connections"][0]["output_price"] == 8.0

    from myink.db import new_session as ns
    from myink.models import Project
    with ns() as db:
        proj = db.get(Project, uuid.UUID(temp_project))
        proj.user_id = uuid.UUID(temp_user)
        db.commit()
    chain = make_chain("writer", project_id=temp_project)
    assert chain.chain == ["novel-pro"]
    assert chain.prices == {"input": 3.5, "input_cache_hit": 3.5, "output": 8.0}


def test_environment_rejects_partial_prices(temp_user):
    cid = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        put_environment(EnvironmentBody(
            model_connections=[ModelConnectionBody(
                id=cid, name="私有", protocol="openai",
                base_url="https://models.example.com/v1", model="novel-pro",
                api_key="k", input_price=3.5,
            )],
        ), user_id=temp_user)
    assert exc.value.status_code == 400
    assert "输入价与输出价" in str(exc.value.detail)


def test_environment_omitting_fields_preserves_them(temp_user):
    cid = str(uuid.uuid4())
    put_environment(EnvironmentBody(
        model_connections=[ModelConnectionBody(
            id=cid, name="私有", protocol="openai",
            base_url="https://models.example.com/v1", model="novel-pro", api_key="k",
        )],
        model_routes={"writer": f"custom:{cid}"},
    ), user_id=temp_user)
    again = put_environment(EnvironmentBody(), user_id=temp_user)
    assert again["model_routes"] == {"writer": f"custom:{cid}"}
    cleared = put_environment(EnvironmentBody(model_routes={}), user_id=temp_user)
    assert cleared["model_routes"] == {}


def test_environment_rejects_invalid_role_and_rankings(temp_user):
    with pytest.raises(HTTPException) as e1:
        put_environment(EnvironmentBody(model_routes={"not-a-role": "custom:x"}), user_id=temp_user)
    assert e1.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        put_environment(EnvironmentBody(rankings={"timeout": 99}), user_id=temp_user)
    assert e2.value.status_code == 400


def test_environment_saves_audit_summarize_and_thinking(temp_user):
    cid = str(uuid.uuid4())
    out = put_environment(EnvironmentBody(
        model_connections=[ModelConnectionBody(
            id=cid, name="私有", protocol="openai",
            base_url="https://models.example.com/v1", model="novel-pro", api_key="k",
        )],
        model_routes={"audit": f"custom:{cid}", "summarize": f"custom:{cid}"},
        thinking_enabled=True,
    ), user_id=temp_user)
    assert out["model_routes"] == {"audit": f"custom:{cid}", "summarize": f"custom:{cid}"}
    assert out["thinking_enabled"] is True
    kept = put_environment(EnvironmentBody(), user_id=temp_user)
    assert kept["thinking_enabled"] is True


def test_make_chain_prefers_user_environment_over_project(temp_project, temp_user):
    from myink.db import new_session as ns
    from myink.models import Project

    with ns() as db:
        proj = db.get(Project, uuid.UUID(temp_project))
        proj.user_id = uuid.UUID(temp_user)
        db.commit()

    proj_cid = str(uuid.uuid4())
    put_project_settings(temp_project, SettingsBody(
        model_connections=[ModelConnectionBody(
            id=proj_cid, name="书内", protocol="openai",
            base_url="https://book.example.com/v1", model="book-model", api_key="book-key",
        )],
        model_routes={"writer": f"custom:{proj_cid}"},
    ))
    assert make_chain("writer", project_id=temp_project).chain == ["book-model"]

    env_cid = str(uuid.uuid4())
    put_environment(EnvironmentBody(
        model_connections=[ModelConnectionBody(
            id=env_cid, name="账号", protocol="openai",
            base_url="https://env.example.com/v1", model="env-model", api_key="env-key",
        )],
        model_routes={"writer": f"custom:{env_cid}"},
    ), user_id=temp_user)
    assert make_chain("writer", project_id=temp_project).chain == ["env-model"]


def test_probe_reuses_environment_connection_key(temp_user, monkeypatch):
    cid = str(uuid.uuid4())
    put_environment(EnvironmentBody(
        model_connections=[ModelConnectionBody(
            id=cid, name="私有", protocol="openai",
            base_url="https://models.example.com/v1", model="novel-pro", api_key="stored-secret")],
    ), user_id=temp_user)
    seen: dict = {}

    def fake_list(protocol, base_url, api_key):
        seen["key"] = api_key
        return ["novel-pro"], None

    monkeypatch.setattr(routes_environment.probe, "list_models", fake_list)
    out = routes_environment.list_environment_models(
        routes_environment.ConnectionProbeBody(
            protocol="openai", base_url="https://models.example.com/v1", connection_id=cid,
        ),
        user_id=temp_user,
    )
    assert out["ok"] is True and seen["key"] == "stored-secret"


def test_rankings_probe_reports_tools(temp_user, monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def list_tools(self):
            return ["qidian_rank", "community_rank"]

    monkeypatch.setattr(routes_environment, "McpClient", lambda *a, **k: FakeClient())
    out = __import__("asyncio").run(routes_environment.test_rankings_connection(
        routes_environment.RankingsProbeBody(mcp_url="https://mcp.example.com/api", timeout=5),
        user_id=temp_user,
    ))
    assert out == {"ok": True, "tools": ["qidian_rank", "community_rank"], "error": None}


def test_environment_requires_auth():
    from fastapi.testclient import TestClient

    from myink.api.main import app

    assert TestClient(app).get("/internal/v1/environment").status_code == 403


def test_environment_requires_auth():
    from fastapi.testclient import TestClient

    from myink.api.main import app

    client = TestClient(app)
    assert client.get("/internal/v1/environment").status_code == 403


def test_save_raw_unknown_user():
    with pytest.raises(LookupError):
        save_raw(uuid.uuid4(), {})
