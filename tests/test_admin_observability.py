"""Capture bounds/redaction must never modify live provider data."""
import copy
import importlib.util
import json
import uuid
from time import perf_counter
from contextlib import contextmanager

import pytest

from myink.providers import ModelResponse
from myink.workflow import nodes


def test_capture_bounds_credentials_and_original_data():
    assert importlib.util.find_spec("myink.admin_observability") is not None
    from myink.admin_observability import capture
    original = {"password_hash": "HASH-SECRET", "apiKey": "KEY-SECRET", "input_tokens": 123,
                "nested": [{"Authorization": "Bearer AUTH-SECRET", "text": "postgres://me:URL-SECRET@db/a"}],
                "text": "Bearer TEXT-SECRET api_key=INLINE-SECRET", "many": ["x"*10000]*500}
    before = copy.deepcopy(original)
    result = capture(original)
    encoded = json.dumps(result)
    assert not any(secret in encoded for secret in ("HASH-SECRET", "KEY-SECRET", "AUTH-SECRET", "URL-SECRET", "TEXT-SECRET", "INLINE-SECRET"))
    assert result["data"]["input_tokens"] == 123
    assert result["truncated"] and result["redacted"]
    assert len(encoded.encode()) <= 70000
    assert original == before
    cyclic = {}; cyclic["self"] = cyclic
    assert capture(cyclic)["truncated"]


def test_long_text_followed_by_public_url_has_bounded_scrubbing_time():
    from myink.admin_observability import capture
    original = "a" * 20000 + " https://example.com"
    started = perf_counter()
    result = capture(original)
    elapsed = perf_counter() - started
    # The former unanchored scheme regex rescanned the long non-URL prefix and
    # took seconds here. Linear scrubbing takes milliseconds; 1s leaves margin.
    assert elapsed < 1.0, f"URL scrubbing took {elapsed:.3f}s for 20KiB"
    assert result["truncated"] and not result["redacted"]
    assert len(json.dumps(result).encode()) <= 65536


def test_url_credentials_are_redacted_before_clipping_without_touching_public_urls():
    from myink.admin_observability import capture, scrub_text
    value = ("https://example.com/a userinfo "
             "postgresql+psycopg://alice:URL-SECRET@db.local:5432/story "
             "redis://:REDIS-SECRET@cache.local/0 "
             "https://only-user@host.local/a")
    cleaned = scrub_text(value)
    assert "https://example.com/a" in cleaned
    assert "URL-SECRET" not in cleaned and "REDIS-SECRET" not in cleaned and "only-user" not in cleaned
    assert "postgresql+psycopg://[REDACTED]@db.local:5432/story" in cleaned
    assert "redis://[REDACTED]@cache.local/0" in cleaned
    long_url = "https://alice:" + "LONG-SECRET" * 5000 + "@host.local/path"
    captured = capture(long_url)
    assert captured["data"] == "https://[REDACTED]@host.local/path"
    assert captured["redacted"] and not captured["truncated"]


def test_adjacent_json_url_credentials_are_all_scrubbed():
    from myink.admin_observability import scrub_text
    original = json.dumps(["postgres://alice:FIRST_PRIVATE@db", "redis://bob:SECOND_PRIVATE@cache"],
                          separators=(",", ":"))
    assert json.loads(scrub_text(original)) == ["postgres://[REDACTED]@db", "redis://[REDACTED]@cache"]


@pytest.mark.parametrize("separator", [",", ";", "','"])
def test_adjacent_url_delimiters_cannot_hide_the_next_credentials(separator):
    from myink.admin_observability import scrub_text
    original = "postgres://alice:FIRST_PRIVATE@db" + separator + "redis://bob:SECOND_PRIVATE@cache"
    assert scrub_text(original) == "postgres://[REDACTED]@db" + separator + "redis://[REDACTED]@cache"


def test_adjacent_public_url_does_not_hide_following_private_url():
    from myink.admin_observability import scrub_text
    assert scrub_text("https://example.com,redis://bob:SECOND_PRIVATE@cache") == (
        "https://example.com,redis://[REDACTED]@cache")


def test_actual_provider_messages_outputs_and_detail_merge_are_captured():
    class Collector:
        def __init__(self): self.rows = []
        def add(self, row): self.rows.append(row)
    class Chain:
        chain = []
        def generate(self, messages, **kwargs):
            assert messages[0]["content"] == "context Bearer ORIGINAL-SECRET"
            return ModelResponse(content="<b>original story</b>", model_id="fake", tool_calls=[])
    db = Collector()
    messages = [{"role":"user", "content":"context Bearer ORIGINAL-SECRET"}]
    response, _ = nodes._llm(db, {"project_id":str(uuid.uuid4()), "task_id":str(uuid.uuid4())},
                             "write", "Writer", Chain(), messages, json_mode=False)
    assert response.content == "<b>original story</b>"
    assert messages[0]["content"].endswith("ORIGINAL-SECRET")
    detail = db.rows[0].detail
    assert detail and detail["messages"][0]["content"].startswith("context ")
    assert "ORIGINAL-SECRET" not in json.dumps(detail)
    assert detail["response"]["content"] == response.content


def test_named_session_credentials_and_stored_truncation_survive_merges():
    from myink.admin_observability import capture, capture_detail
    result = capture({"auth_token": "AUTH-SUFFIX", "sessionToken": "SESSION-SUFFIX",
                      "output_tokens": 5, "nested": "apiKey=INLINE-CAMEL"})
    assert not any(s in json.dumps(result) for s in ("AUTH-SUFFIX", "SESSION-SUFFIX", "INLINE-CAMEL"))
    assert result["data"]["output_tokens"] == 5
    first = capture_detail({"messages": [{"content": "x"*30000}]*80, "response": {"content": "actual response"}})
    merged = capture_detail({**first, "audit_verdict": {"decision": "pass", "password": "MERGE-SECRET"}, "tool_trace": [{"tool":"lookup"}]})
    assert merged["audit_verdict"]["decision"] == "pass"
    assert merged["tool_trace"] == [{"tool":"lookup"}]
    assert merged["_capture"]["truncated"] is True
    # Existing control data stays lossless on disk; inspection is still scrubbed.
    assert merged["audit_verdict"]["password"] == "MERGE-SECRET"
    assert "MERGE-SECRET" not in json.dumps(capture(merged))
    assert "response" in first and first["response"]["content"] == "actual response"


def test_tool_round_and_final_record_exact_inputs_and_outputs(monkeypatch):
    class Collector:
        def __init__(self): self.rows = []
        def add(self, row): self.rows.append(row)
    class Chain:
        chain = []
        def generate(self, messages, **kwargs):
            if len(messages) == 1:
                return ModelResponse(content="checking", model_id="fake", tool_calls=[{"id":"c1", "name":"lookup", "arguments":{"token":"TOOL-SECRET"}}])
            assert messages[2]["content"] == "recalled Bearer TOOL-RESULT-SECRET"
            return ModelResponse(content="final story", model_id="fake")
    monkeypatch.setattr(nodes, "execute_tool", lambda *args: "recalled Bearer TOOL-RESULT-SECRET")
    db = Collector()
    response, trace = nodes._run_tool_loop(db, {"project_id":str(uuid.uuid4()), "task_id":str(uuid.uuid4())},
                      "write", "Writer", Chain(), [{"role":"user", "content":"write it"}],
                      max_tokens=100, tools=[{"type":"function"}], max_tool_calls=1, final_json=False)
    assert response.content == "final story" and len(db.rows) == 2
    assert trace[0]["result"] == "recalled Bearer TOOL-RESULT-SECRET"
    assert len(db.rows[0].detail["messages"]) == 1 and len(db.rows[1].detail["messages"]) == 4
    assert db.rows[0].detail["response"]["tool_calls"][0]["name"] == "lookup"
    assert db.rows[1].detail["response"]["content"] == "final story"
    assert "TOOL-SECRET" not in json.dumps([r.detail for r in db.rows])
    assert "TOOL-RESULT-SECRET" not in json.dumps([r.detail for r in db.rows])


def test_recall_and_refreshed_cast_capture_actual_context(temp_project, monkeypatch):
    from myink.db import tenant_session
    from myink.models import AgentRun, Character
    from test_plan_cast import _CastProvider
    import myink.providers as providers
    with tenant_session(temp_project) as db:
        db.add(Character(project_id=uuid.UUID(temp_project), name="林砚", realm_cap="金丹"))
    monkeypatch.setattr(providers, "default_provider", _CastProvider(["林砚"], ["山门"]))
    state = {"project_id":temp_project, "task_id":str(uuid.uuid4()), "chapter_seq":1, "characters":[{"name":"林砚"}]}
    recalled = nodes.node_recall(state)
    refreshed = nodes.node_plan_cast({**state, **recalled})
    with tenant_session(temp_project) as db:
        rows = db.query(AgentRun).filter(AgentRun.task_id == state["task_id"]).order_by(AgentRun.id).all()
        recall = next(r for r in rows if r.node == "recall")
        assert recall.detail["context"] == recalled["context"]
        cast_context = [r for r in rows if r.node == "plan_cast" and "context" in r.detail][0]
        assert cast_context.detail["context"] == refreshed["context"]
        assert cast_context.detail["context"]["entity_snapshots"][0]["name"] == "林砚"


def test_oversized_control_details_remain_lossless_for_later_consumers():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from myink.admin_observability import capture
    from myink.models import AgentRun
    from myink.worker.processor import _latest_plan_attempt

    # A real isolated ORM store exercises reload/merge/query behavior without
    # relying on a fake query object or using any shared database fixture.
    engine = create_engine("sqlite:///:memory:")
    AgentRun.__table__.create(engine)
    pid, task_id, batch_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    messages = [{"role": "user", "content": "large prompt " * 3000}] * 100
    plan = {"goals": [f"goal-{i}:" + "scene " * 500 for i in range(100)],
            "password": "LEGACY-PLAN-SECRET"}
    findings = [{"conflict_key": f"case-{i}", "conflict_type": "power", "severity": "major",
                 "suggestion": f"finding-{i}:" + "detail " * 500} for i in range(100)]
    verdict = {"verdict": "replan_chapter", "findings": findings, "password": "LEGACY-AUDIT-SECRET"}
    before = copy.deepcopy((plan, verdict, messages))
    try:
        with Session(engine, autoflush=False) as db:
            response = ModelResponse(content="original response", model_id="fake")
            nodes.record_run(db, project_id=pid, task_id=task_id, node="plan_chapter", role="Planner",
                             resp=response, messages=messages)
            nodes.record_run_detail(db, task_id=task_id, node="plan_chapter",
                                    detail={"plan": plan, "plan_attempt": 7, "writing_mode": "manual"})
            nodes.record_run(db, project_id=pid, task_id=batch_id+":ch1", node="audit", role="Audit",
                             resp=response, messages=messages)
            nodes.record_run_detail(db, task_id=batch_id+":ch1", node="audit", detail={"audit_verdict": verdict})
            db.commit()
            db.expire_all()
            assert _latest_plan_attempt(db, task_id) == 7
            plan_row = db.query(AgentRun).filter(AgentRun.task_id == task_id).one()
            assert plan_row.detail["plan"] == plan and plan_row.detail["writing_mode"] == "manual"
            audit_row = db.query(AgentRun).filter(AgentRun.task_id == batch_id+":ch1").one()
            assert audit_row.detail["audit_verdict"] == verdict
            expected = [{**finding, "_chapter": 1} for finding in findings]
            assert nodes._collect_batch_audit_findings(db, batch_id) == expected
            assert nodes._collect_chapter_window_findings(db, pid, 1, 1) == expected
            for row in (plan_row, audit_row):
                bounded = capture(row.detail)
                assert bounded["truncated"]
                assert len(json.dumps(bounded).encode()) <= 65536
                assert "LEGACY-PLAN-SECRET" not in json.dumps(bounded)
                assert "LEGACY-AUDIT-SECRET" not in json.dumps(bounded)
                telemetry = {key: value for key, value in row.detail.items()
                             if key not in {"plan", "plan_attempt", "writing_mode", "audit_verdict"}}
                assert len(json.dumps(telemetry).encode()) <= 65536
                assert row.detail["_capture"]["scope"] == "telemetry_only"
        assert (plan, verdict, messages) == before
    finally:
        engine.dispose()
