"""全局审计：对照卷规划判断推进是否偏离。"""

from __future__ import annotations

import json
import types
import uuid

import pytest
from fastapi import HTTPException

from myink.db import tenant_session
from myink.models import Chapter, GlobalAuditReport, VolumeOutline
from myink.providers.base import ModelResponse
from myink.validation import global_audit as ga

from test_flow import StubProvider


@pytest.fixture(scope="module", autouse=True)
def _ensure_audit_reports_table():
    from myink.db import ensure_global_audit_reports

    ensure_global_audit_reports()


def _seed_chapter(pid: str, seq: int, content: str) -> None:
    with tenant_session(pid) as db:
        db.add(Chapter(project_id=uuid.UUID(pid), chapter_seq=seq, title=f"第{seq}章",
                       content=content, status="confirmed", version=1))
        db.commit()


def _seed_outline(pid: str, *, start: int = 1, end: int = 30, goal: str = "入宗立足") -> None:
    with tenant_session(pid) as db:
        db.add(VolumeOutline(
            project_id=uuid.UUID(pid), volume_seq=1, title="整书大纲",
            outline={
                "objective": "成为宗门长老",
                "chapter_count": end,
                "volumes": [{
                    "title": "第一卷 · 青云山下",
                    "goal": goal,
                    "key_results": ["通过试炼"],
                    "end_event": "被迫离宗",
                    "chapter_start": start,
                    "chapter_end": end,
                    "stages": [{
                        "name": "前期",
                        "chapter_start": start,
                        "chapter_end": end,
                        "goal": "入门试炼",
                        "beats": ["得玉佩"],
                    }],
                }],
            }))
        db.commit()


def _report_rows(pid: str) -> list[GlobalAuditReport]:
    with tenant_session(pid) as db:
        return db.query(GlobalAuditReport).filter(
            GlobalAuditReport.project_id == uuid.UUID(pid)).order_by(
            GlobalAuditReport.window_start).all()


class _Chain:
    def __init__(self, provider):
        self.provider = provider

    def generate(self, messages, *, json_mode=False, max_tokens=None, temperature=None, tools=None):
        return self.provider.generate(messages, model_id="stub", max_tokens=max_tokens,
                                      temperature=temperature, json_mode=json_mode, tools=tools)


class AuditStub:
    def __init__(self, findings=None, error=None):
        self.findings = findings if findings is not None else []
        self.error = error
        self.calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        self.calls += 1
        if self.error:
            return ModelResponse(content="", model_id=model_id, error=self.error,
                                 input_tokens=50, output_tokens=0, duration_ms=30)
        return ModelResponse(content=json.dumps({"findings": self.findings}, ensure_ascii=False),
                             model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)


def _install_audit_stub(monkeypatch, stub):
    monkeypatch.setattr(ga, "make_chain", lambda role, **_kwargs: _Chain(stub))


def test_window_threshold_triggers(temp_project):
    for seq in range(1, 6):
        _seed_chapter(temp_project, seq, f"正文第{seq}章")
    with tenant_session(temp_project) as db:
        assert ga.window_for_batch(db, temp_project, K=10) is None
    for seq in range(6, 11):
        _seed_chapter(temp_project, seq, f"正文第{seq}章")
    with tenant_session(temp_project) as db:
        assert ga.window_for_batch(db, temp_project, K=10) == (1, 10)


def test_window_resumes_after_prior_report(temp_project):
    for seq in range(1, 21):
        _seed_chapter(temp_project, seq, f"林砚静观云海。第{seq}章。")
    with tenant_session(temp_project) as db:
        ga.record_report(db, project_id=temp_project, window=(1, 10), findings=[], sampled=[],
                         status="completed", error=None, source="batch")
        db.commit()
    with tenant_session(temp_project) as db:
        win = ga.window_for_batch(db, temp_project, K=10)
    assert win == (11, 20)


def test_no_outline_skips_llm(temp_project, monkeypatch):
    _seed_chapter(temp_project, 1, "山间雾气弥漫。")
    monkeypatch.setattr(ga, "make_chain", lambda role, **_kwargs: (_ for _ in ()).throw(
        AssertionError("无大纲不应调 LLM")))
    with tenant_session(temp_project) as db:
        rep = ga.run_global_audit(db, temp_project, (1, 1), source="manual")
        db.flush()
    assert rep["status"] == "completed"
    assert rep["findings"] == []
    assert rep["summary"].get("reason") == "no_outline"
    assert rep["audited_up_to_chapter"] == 1


def test_volume_drift_detected(temp_project, monkeypatch):
    _seed_outline(temp_project, end=12)
    for seq in range(1, 12):
        _seed_chapter(temp_project, seq, f"林砚静观云海，谋定后动。第{seq}章。")
    _seed_chapter(temp_project, 12, "林砚一脚踹开房门，破口大骂：'都给我滚！'")
    drift = {"verdict": "drifted", "chapter": 12, "volume_seq": 1, "stage_seq": 1,
             "evidence": "一脚踹开房门，破口大骂", "reason": "偏离入门试炼",
             "recovery": "下一阶段拉回宗门线", "confidence": 0.85}
    _install_audit_stub(monkeypatch, AuditStub(findings=[drift]))
    with tenant_session(temp_project) as db:
        rep = ga.run_global_audit(db, temp_project, (1, 12), source="manual")
        db.flush()
    assert rep["status"] == "completed"
    assert len(rep["findings"]) == 1
    f = rep["findings"][0]
    assert f["conflict_type"] == "volume" and f["severity"] == "hint"
    assert f["scope"] == "structural" and f["source"] == "L2"
    assert f["evidence"][0]["chapter"] == 12
    assert "拉回宗门线" in f["suggestion"]


def test_volume_on_track_empty(temp_project, monkeypatch):
    _seed_outline(temp_project, end=10)
    for seq in range(1, 11):
        _seed_chapter(temp_project, seq, f"林砚静观云海，谋定后动。第{seq}章。")
    _install_audit_stub(monkeypatch, AuditStub(findings=[]))
    with tenant_session(temp_project) as db:
        rep = ga.run_global_audit(db, temp_project, (1, 10), source="manual")
        db.flush()
    assert rep["status"] == "completed"
    assert rep["findings"] == [] and rep["summary"]["findings"] == 0


def test_overflagging_guard_zero_false_positive(temp_project, monkeypatch):
    _seed_outline(temp_project, end=1)
    _seed_chapter(temp_project, 1, "林砚立于船头，静观云海。")
    bad = [
        {"verdict": "drifted", "chapter": 1, "evidence": "这段引文根本不在正文里",
         "reason": "r", "recovery": "x", "confidence": 0.9},
        {"verdict": "drifted", "chapter": 99, "evidence": "林砚立于船头",
         "reason": "r", "recovery": "x", "confidence": 0.9},
        {"verdict": "ok", "chapter": 1, "evidence": "林砚立于船头",
         "reason": "r", "recovery": "x", "confidence": 0.9},
        {"verdict": "drifted", "chapter": 1, "evidence": "林砚立于船头",
         "reason": "r", "recovery": "x", "confidence": 0.4},
    ]
    _install_audit_stub(monkeypatch, AuditStub(findings=bad))
    with tenant_session(temp_project) as db:
        rep = ga.run_global_audit(db, temp_project, (1, 1), source="manual")
        db.flush()
    assert rep["status"] == "completed"
    assert rep["findings"] == []


def test_llm_failure_non_blocking(temp_project, monkeypatch):
    _seed_outline(temp_project, end=1)
    _seed_chapter(temp_project, 1, "林砚立于船头。")
    _install_audit_stub(monkeypatch, AuditStub(error="audit 判定失败"))
    with tenant_session(temp_project) as db:
        rep = ga.run_global_audit(db, temp_project, (1, 1), source="manual")
        db.flush()
    assert rep["status"] == "failed"
    assert rep["error"] == "audit 判定失败"
    assert rep["findings"] == []
    assert rep["audited_up_to_chapter"] == 1


def test_report_persisted_and_marker_advances(temp_project, monkeypatch):
    _seed_outline(temp_project, end=21)
    for seq in range(1, 11):
        _seed_chapter(temp_project, seq, f"林砚静观云海。第{seq}章。")
    _install_audit_stub(monkeypatch, AuditStub(findings=[]))
    with tenant_session(temp_project) as db:
        rep = ga.run_global_audit(db, temp_project, (1, 10), source="batch",
                                  source_batch_task_id="b0")
        db.flush()
    assert rep["status"] == "completed" and rep["audited_up_to_chapter"] == 10
    rows = _report_rows(temp_project)
    assert len(rows) == 1
    assert rows[0].trigger == "batch" and rows[0].source_batch_task_id == "b0"
    assert rows[0].summary["findings"] == 0 and rows[0].summary["chapters"] == 10
    for seq in (11, 12):
        _seed_chapter(temp_project, seq, f"林砚静观云海。第{seq}章。")
    with tenant_session(temp_project) as db:
        assert ga.window_for_batch(db, temp_project, K=10) is None
    _seed_chapter(temp_project, 21, "林砚静观云海。第21章。")
    with tenant_session(temp_project) as db:
        assert ga.window_for_batch(db, temp_project, K=10) == (11, 21)


def test_batch_summary_surfaces_metrics(temp_project, monkeypatch):
    import myink.providers as providers_mod
    import myink.workflow.batch_graph as bg_mod
    from myink.workflow.batch_graph import build_batch_graph
    from myink.workflow.chapter_graph import build_chapter_graph

    _seed_outline(temp_project, end=5)
    monkeypatch.setattr(bg_mod, "settings", types.SimpleNamespace(audit_interval=1))
    audit_stub = AuditStub(findings=[])
    _install_audit_stub(monkeypatch, audit_stub)
    monkeypatch.setattr(providers_mod, "default_provider", StubProvider("金丹", "金丹"))
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(_BatchPlanStub()))

    thread = str(uuid.uuid4())
    result = build_batch_graph(build_chapter_graph()).invoke(
        {"project_id": temp_project, "batch_task_id": thread, "size": 2,
         "position": 0, "start_chapter": 1},
        config={"configurable": {"thread_id": thread}},
    )
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "done", f"批次应完成，实际 {summary}"
    ga_metric = summary.get("global_audit") or {}
    assert ga_metric.get("window_start") == 1 and ga_metric.get("window_end") == 2
    assert ga_metric.get("status") == "completed"
    assert ga_metric.get("findings") == []
    assert audit_stub.calls == 1


class _BatchPlanStub:
    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        sys = messages[0].get("content", "") if messages else ""
        if "批次规划" in sys:
            return ModelResponse(content=('{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"},'
                                          '{"goal":"推进主线B","outline_advance":"卷纲推进"}]}'),
                                 model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)
        raise AssertionError(f"不应调用: {sys[:30]}")


def test_below_threshold_no_report(temp_project, monkeypatch):
    import myink.workflow.batch_graph as bg_mod

    _seed_chapter(temp_project, 1, "林砚静观云海。")
    monkeypatch.setattr(ga, "make_chain", lambda role, **_kwargs: (_ for _ in ()).throw(
        AssertionError("below_threshold 应短路，不调 LLM")))
    out = bg_mod.node_global_audit({"project_id": temp_project, "batch_task_id": "b0",
                                    "size": 5, "position": 0, "start_chapter": 1})
    assert out == {"global_audit": {"triggered": False, "reason": "below_threshold"}}
    assert len(_report_rows(temp_project)) == 0


def test_endpoint_global_audit(temp_project, monkeypatch):
    from myink.api.routes_global_audit import trigger_global_audit

    _seed_outline(temp_project, end=2)
    _seed_chapter(temp_project, 1, "林砚静观云海。")
    _seed_chapter(temp_project, 2, "林砚谋定后动。")
    _install_audit_stub(monkeypatch, AuditStub(findings=[]))
    rep = trigger_global_audit(temp_project)
    assert rep["status"] == "completed"
    assert rep["window_start"] == 1 and rep["window_end"] == 2
    assert rep["findings"] == []
    rows = _report_rows(temp_project)
    assert len(rows) == 1 and rows[0].trigger == "manual"


def test_endpoint_no_chapters_400(temp_project, monkeypatch):
    from myink.api.routes_global_audit import trigger_global_audit

    with pytest.raises(HTTPException) as ei:
        trigger_global_audit(temp_project)
    assert ei.value.status_code == 400


def test_endpoint_llm_failure_502(temp_project, monkeypatch):
    from myink.api.routes_global_audit import trigger_global_audit

    _seed_outline(temp_project, end=1)
    _seed_chapter(temp_project, 1, "林砚静观云海。")
    _install_audit_stub(monkeypatch, AuditStub(error="audit 判定失败"))
    with pytest.raises(HTTPException) as ei:
        trigger_global_audit(temp_project)
    assert ei.value.status_code == 502
    rows = _report_rows(temp_project)
    assert len(rows) == 1 and rows[0].status == "failed"
