"""reflexion 复盘沉淀测试（§8.9，阶段 3 首切片）。

mock LLM（不依赖真实 DeepSeek key）。验证：
- 批次收尾提炼：audit findings → LLM 总结演化 → writing_lessons 落库（分级 proposed/active）；
- 短路（无发现/已复盘/全被覆盖）与 LLM 失败不阻塞批次；
- 复发率记账（确定性 category 匹配）、同 category 演化更新同一行；
- 注入链：DB → build_context.reflexions → plan/write system 段（通道过滤 + cap）；
- 人工确认/拒绝 + 确认 API。

设计：批次级 stub（batch_plan + reflexion）走 batch_graph.make_chain monkeypatch，
按 system 唯一标记分流（"批次规划" / "复盘 Agent"）；单章子图走 default_provider
stub（章节 audit 按章号返回 findings，verdict=pass 让章正常 persist 完成）。
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi import HTTPException

from myink.db import tenant_session
from myink.memory.recall import build_context
from myink.models import WritingLesson
from myink.providers.base import ModelResponse
from myink.workflow import nodes, prompts
from myink.workflow.batch_graph import build_batch_graph, node_reflexion
from myink.workflow.chapter_graph import build_chapter_graph

from test_flow import StubProvider


class _Chain:
    """批次级链包装（batch_plan/reflexion 共用，仿 test_flow._Chain）。"""

    def __init__(self, provider):
        self.provider = provider

    def generate(self, messages, *, json_mode=False, max_tokens=None, temperature=None, tools=None):
        return self.provider.generate(messages, model_id="stub", max_tokens=max_tokens,
                                      temperature=temperature, json_mode=json_mode, tools=tools)


class ChapterAuditStub(StubProvider):
    """单章 stub：audit 按章号返回 findings，verdict=pass（章正常 persist 完成）。

    ch1 → major power（战力越界）；ch2 → minor style（文风堆砌）。findings 经
    record_run_detail 落 agent_runs.detail，reflexion 据此收集。
    """

    FINDINGS = {
        1: [{"conflict_key": "p1", "conflict_type": "power", "severity": "major", "scope": "structural",
             "evidence": [{"chapter": 1, "quote": "林砚一掌拍出，直接晋升大境界"}],
             "confidence": 0.8, "suggestion": "突破须有契机，不可凭空晋级"}],
        2: [{"conflict_key": "s1", "conflict_type": "style", "severity": "minor", "scope": "local",
             "evidence": [{"chapter": 2, "quote": "夜色沉沉，月色沉沉，心绪沉沉"}],
             "confidence": 0.7, "suggestion": "减少排比堆砌，对话区分腔调"}],
    }

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        if self._infer_node(messages) == "audit":
            user = "\n".join(m.get("content", "") for m in messages)
            m = re.search(r"第 (\d+) 章", user)
            ch = int(m.group(1)) if m else 1
            findings = self.FINDINGS.get(ch, [])
            content = json.dumps({"verdict": "pass", "findings": findings,
                                  "reasons": ["审核完成"], "confidence": 0.85})
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode, tools=tools,
                                disable_thinking=disable_thinking)


class ReflexionBatchStub(StubProvider):
    """批次级 stub：batch_plan 返回 2 章蓝图；reflexion 返回预置 lessons（或 error）。"""

    def __init__(self, realm_from, realm_to, lessons=None, reflexion_error=None):
        super().__init__(realm_from, realm_to)
        self.lessons = lessons if lessons is not None else {"lessons": []}
        self.reflexion_error = reflexion_error
        self.reflexion_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        sys = messages[0].get("content", "") if messages else ""
        if "批次规划" in sys:
            content = ('{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"},'
                       '{"goal":"推进主线B","outline_advance":"卷纲推进"}]}')
            return ModelResponse(content=content, model_id=model_id, input_tokens=50,
                                 output_tokens=80, duration_ms=30)
        if "复盘 Agent" in sys:
            self.reflexion_calls += 1
            if self.reflexion_error:
                return ModelResponse(content="", model_id=model_id, error=self.reflexion_error,
                                     input_tokens=50, output_tokens=0, duration_ms=30)
            return ModelResponse(content=json.dumps(self.lessons, ensure_ascii=False),
                                 model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode, tools=tools,
                                disable_thinking=disable_thinking)


def _install(monkeypatch, chapter_stub, batch_stub):
    """装两个 stub：default_provider（单章）+ batch_graph.make_chain（batch_plan/reflexion）。"""
    import myink.providers as providers_mod
    import myink.workflow.batch_graph as bg_mod

    monkeypatch.setattr(providers_mod, "default_provider", chapter_stub)
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(batch_stub))
    return bg_mod


def _run_batch(project_id: str, monkeypatch, batch_stub=None,
               chapter_stub=None, size: int = 2, start: int = 1):
    """跑一批（batch_plan + 单章×size + reflexion + batch_end），返回 (result, bg_mod)。"""
    chapter_stub = chapter_stub or ChapterAuditStub("金丹", "金丹")
    batch_stub = batch_stub or ReflexionBatchStub(
        "金丹", "金丹",
        lessons={"lessons": [
            {"conflict_type": "power", "lesson_type": "both", "content": "突破须有契机且配角战力受限",
             "confidence": 0.9},
            {"conflict_type": "style", "lesson_type": "writing", "content": "减少排比堆砌，对话区分腔调",
             "confidence": 0.8},
        ]},
    )
    bg_mod = _install(monkeypatch, chapter_stub, batch_stub)
    thread = str(uuid.uuid4())
    from langgraph.checkpoint.memory import InMemorySaver
    from myink.workflow.batch_graph import BatchReviewError
    from myink.workflow.runner import resume_thread
    from myink.models import MemoryCandidate
    checkpoint = InMemorySaver()
    graph = build_batch_graph(build_chapter_graph(checkpointer=checkpoint), checkpointer=checkpoint)
    initial = {"project_id": project_id, "batch_task_id": thread, "size": size,
               "position": 0, "start_chapter": start}
    try:
        result = graph.invoke(
        {"project_id": project_id, "batch_task_id": thread, "size": size,
         "position": 0, "start_chapter": start},
            config={"configurable": {"thread_id": thread}},
        )
    except BatchReviewError:
        # 审核同时给 pass+major 不再自动放行。复盘测试显式模拟作者接受当前稿、
        # 拒绝候选入库后的续跑，保留已评审的 major 供复盘分级。
        with tenant_session(project_id) as db:
            for candidate in db.query(MemoryCandidate).filter_by(status="pending").all():
                candidate.status = "rejected"
                candidate.review = {"mode": "memory_only"}
        result = resume_thread(graph, thread, initial)
    return result, bg_mod, thread


def _reflexion_runs(project_id: str, batch_task_id: str):
    from sqlalchemy import select

    from myink.models import AgentRun

    with tenant_session(project_id) as db:
        rows = db.execute(select(AgentRun).where(
            AgentRun.task_id == batch_task_id, AgentRun.node == "reflexion")
        ).scalars().all()
        return [{"detail": r.detail, "error": r.error} for r in rows]


def _count_lessons(project_id: str) -> int:
    from sqlalchemy import func

    from myink.models import AgentRun  # noqa: F401  (RLS 覆盖 writing_lessons，同一会话即可)

    with tenant_session(project_id) as db:
        return db.query(WritingLesson).filter(
            WritingLesson.project_id == uuid.UUID(project_id)).count()


def _make_lesson(pid: uuid.UUID, **kw):
    base = dict(project_id=pid, content="测试经验", content_hash="h", evidence=[],
                confidence=0.9, source_chapter=1, source_batch_task_id="b0", status="active")
    base.update(kw)
    return WritingLesson(**base)


# ---- 1. 批次收尾提炼 + 分级落库（集成）----


def test_batch_reflexion_distills_and_grades(temp_project, monkeypatch):
    result, _, thread = _run_batch(temp_project, monkeypatch)
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "done", f"批次应完成，实际 {summary}"
    ref = summary.get("reflexion") or {}
    assert ref.get("lessons") == 2, f"应提炼 2 条经验，实际 {ref}"
    assert ref.get("findings") == 2, "本批共 2 项发现（ch1 power + ch2 style）"

    with tenant_session(temp_project) as db:
        rows = db.query(WritingLesson).filter(
            WritingLesson.project_id == uuid.UUID(temp_project)).all()
        assert len(rows) == 2
        by_cat = {r.category: r for r in rows}
        assert by_cat["power"].status == "proposed", "major → 待人工确认"
        assert by_cat["style"].status == "active", "minor → 自动生效"
        assert by_cat["power"].source_chapter == 1
        assert by_cat["style"].source_chapter == 2


# ---- 2. 无发现短路（不调 LLM）----


def test_reflexion_no_findings_shortcircuits(temp_project, monkeypatch):
    batch_stub = ReflexionBatchStub("金丹", "金丹")
    result, _, thread = _run_batch(temp_project, monkeypatch,
                                   chapter_stub=StubProvider("金丹", "金丹"),
                                   batch_stub=batch_stub)
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "done"
    assert (summary.get("reflexion") or {}).get("reason") == "no_new_findings"
    assert batch_stub.reflexion_calls == 0, "无发现应短路，不调 LLM"
    assert _count_lessons(temp_project) == 0, "无发现不应落库经验"
    runs = _reflexion_runs(temp_project, thread)
    assert len(runs) == 1
    assert (runs[0]["detail"] or {}).get("reason") == "no_new_findings"


# ---- 3. LLM 失败不阻塞批次 ----


def test_reflexion_llm_failure_does_not_block_batch(temp_project, monkeypatch):
    batch_stub = ReflexionBatchStub("金丹", "金丹", reflexion_error="reflexion 提炼失败")
    result, _, thread = _run_batch(temp_project, monkeypatch, batch_stub=batch_stub)
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "done", "复盘失败不应使批次 failed"
    assert "error" in (summary.get("reflexion") or {})
    assert _count_lessons(temp_project) == 0, "LLM 失败不应落库经验"
    runs = _reflexion_runs(temp_project, thread)
    assert runs and runs[0]["error"] == "reflexion 提炼失败"


# ---- 4. 同批重跑幂等（guard 短路）----


def test_reflexion_batch_rerun_idempotent(temp_project, monkeypatch):
    _, bg_mod, thread = _run_batch(temp_project, monkeypatch)
    assert _count_lessons(temp_project) == 2
    # 二次 node_reflexion：_batch_already_reflexed 命中 → 短路，不再调 LLM
    orig_make_chain = bg_mod.make_chain
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: (_ for _ in ()).throw(
        AssertionError("幂等 guard 应短路，不调 make_chain")))
    out = node_reflexion({"project_id": temp_project, "batch_task_id": thread,
                          "start_chapter": 1, "size": 2})
    assert out == {"reflexion": {"skipped": "already_reflexed"}}
    monkeypatch.setattr(bg_mod, "make_chain", orig_make_chain)
    assert _count_lessons(temp_project) == 2, "重跑不应新增经验"


# ---- 5. 复发率记账（确定性 category 匹配）----


def test_reflexion_recurrence_metric(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        l = _make_lesson(pid, category="power", lesson_type="both", content="突破须有契机",
                         content_hash="h1", source_chapter=1, status="active", recurrence_count=0)
        # 本书第 2 章才确立的经验（source_chapter >= 本批首章）→ 本批内同类发现不算复发
        l2 = _make_lesson(pid, category="plotline", lesson_type="planning", content="主线每3章推进",
                          content_hash="h2", source_chapter=2, status="active", recurrence_count=0)
        db.add_all([l, l2])
        db.flush()
        n = nodes._update_recurrences(db, temp_project, [
            {"conflict_type": "power", "_chapter": 5},
            {"conflict_type": "power", "_chapter": 5},   # 同 (lesson, chapter) 去重
            {"conflict_type": "style", "_chapter": 6},   # 无对应 lesson 不计数
            {"conflict_type": "plotline", "_chapter": 4},  # source_chapter=2 非 < start=2 → 不算
        ], start_chapter=2)
        db.flush()
        db.refresh(l)
        db.refresh(l2)
        assert n == 1, "仅第 5 章 power 复发记一次"
        assert l.recurrence_count == 1 and l.last_recurrence_at == 5
        assert l2.recurrence_count == 0, "本批内确立的经验不算本批复发"


# ---- 6. 同 category 演化更新同一行（不新建）----


def test_reflexion_evolves_existing(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        l = _make_lesson(pid, category="power", lesson_type="both",
                         content="旧版：突破须有契机", content_hash="oldhash",
                         evidence=[{"chapter": 1, "quote": "旧证据"}], confidence=0.8,
                         source_chapter=1, source_batch_task_id="b0",
                         status="active", recurrence_count=3, last_recurrence_at=10)
        db.add(l)
        db.flush()
        inserted, skipped = nodes._persist_lessons(db, temp_project, "b1", 3, [
            {"conflict_type": "power", "lesson_type": "planning",
             "content": "新版：突破须有契机且配角战力受限", "confidence": 0.9},
        ], [
            {"conflict_type": "power", "severity": "major", "_chapter": 3,
             "chapter": 3, "quote": "新证据"},
        ])
        db.flush()
        assert inserted == 1 and skipped == 0
        rows = db.query(WritingLesson).filter(WritingLesson.project_id == pid).all()
        assert len(rows) == 1, "演化应更新同一行，不新建"
        assert rows[0].content == "新版：突破须有契机且配角战力受限"
        assert rows[0].content_hash != "oldhash", "content_hash 随演化更新"
        assert rows[0].status == "active", "演化保持原状态"
        assert rows[0].recurrence_count == 3, "复发指标跨演化继承"
        assert len(rows[0].evidence) == 2, "evidence 追加而非覆盖"


# ---- 7. 注入链：build_context → plan/write system 段（通道过滤）----


def test_lessons_injected_into_plan_and_write(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        db.add_all([
            _make_lesson(pid, category="power", lesson_type="planning", content="突破须有契机",
                         content_hash="h1", source_chapter=1),
            _make_lesson(pid, category="style", lesson_type="writing", content="对话区分腔调",
                         content_hash="h2", source_chapter=2),
            _make_lesson(pid, category="plotline", lesson_type="both", content="主线每3章推进",
                         content_hash="h3", source_chapter=3),
        ])
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=10)
        assert len(ctx.reflexions) == 3, "active 经验应全量进入 recall 上下文"
        ctxd = ctx.model_dump()
    plan_sys = prompts.plan_messages(ctxd, None)[0]["content"]
    write_sys = prompts.write_messages(ctxd, {})[0]["content"]
    assert "突破须有契机" in plan_sys and "对话区分腔调" not in plan_sys, "planning 只进 plan 通道"
    assert "对话区分腔调" in write_sys and "突破须有契机" not in write_sys, "writing 只进 write 通道"
    assert "主线每3章推进" in plan_sys and "主线每3章推进" in write_sys, "both 双通道都进"


# ---- 8. 注入 cap（8 条上限，兼容召回预算）----


def test_lessons_injection_cap(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        db.add_all([
            _make_lesson(pid, category=f"cat{i}", lesson_type="both", content=f"经验{i}",
                         content_hash=f"h{i}", source_chapter=i)
            for i in range(12)
        ])
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=13)
    assert len(ctx.reflexions) <= 8, "注入上限 8 条，防撑爆召回预算"


# ---- 9. 人工确认 / 拒绝（proposed → active / rejected）----


def test_confirm_lesson_and_reject(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        p = _make_lesson(pid, category="power", lesson_type="both", content="突破须有契机",
                         content_hash="h1", source_chapter=1, status="proposed")
        r = _make_lesson(pid, category="style", lesson_type="writing", content="减少排比",
                         content_hash="h2", source_chapter=2, status="proposed")
        db.add_all([p, r])
        db.flush()
        confirmed = nodes.confirm_lesson(db, temp_project, p.id)
        assert confirmed is not None and confirmed.status == "active"
        assert nodes.confirm_lesson(db, temp_project, p.id) is None, "重复确认 → None（幂等）"
        rejected = nodes.reject_lesson(db, temp_project, r.id)
        assert rejected is not None and rejected.status == "rejected"
        assert nodes.reject_lesson(db, temp_project, r.id) is None, "重复拒绝 → None（幂等）"
        assert nodes.confirm_lesson(db, temp_project, r.id) is None, "rejected 不可 confirm"
        db.flush()
        by_status = {x.status for x in db.query(WritingLesson).filter(
            WritingLesson.project_id == pid).all()}
        assert by_status == {"active", "rejected"}


# ---- 10. 确认 API（list / confirm / reject，非 proposed → 409）----


def test_lessons_api(temp_project):
    from myink.api.routes_lessons import confirm_lesson as api_confirm
    from myink.api.routes_lessons import list_lessons, reject_lesson as api_reject

    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        p = _make_lesson(pid, category="power", lesson_type="both", content="突破须有契机",
                         content_hash="h1", source_chapter=1, status="proposed")
        r = _make_lesson(pid, category="style", lesson_type="writing", content="减少排比",
                         content_hash="h2", source_chapter=2, status="proposed")
        db.add_all([p, r])
        db.flush()
        plid, rlid = str(p.id), str(r.id)

    rows = list_lessons(temp_project)
    assert any(row["lesson_id"] == plid and row["status"] == "proposed" for row in rows)
    assert any(row["lesson_id"] == rlid for row in rows)

    assert api_confirm(temp_project, plid)["status"] == "active"
    with pytest.raises(HTTPException) as ei:
        api_confirm(temp_project, plid)  # 已 active → 409
    assert ei.value.status_code == 409

    assert api_reject(temp_project, rlid)["status"] == "rejected"
    with pytest.raises(HTTPException) as ei:
        api_reject(temp_project, rlid)  # 已 rejected → 409
    assert ei.value.status_code == 409

    rows = list_lessons(temp_project, status="active")
    assert any(row["lesson_id"] == plid for row in rows)
    rows = list_lessons(temp_project, status="rejected")
    assert any(row["lesson_id"] == rlid for row in rows)


# ---- 11. 单章流每 N 章复盘（reflexion_for_chapter_window，§8.9 扩展）----


class WindowReflexionChain:
    """nodes.make_chain("audit") 换桩：reflexion 返回预置 lessons（或 error）。"""

    def __init__(self, lessons=None, reflexion_error=None):
        self.lessons = lessons if lessons is not None else {"lessons": []}
        self.reflexion_error = reflexion_error
        self.calls = 0

    def generate(self, messages, *, json_mode=False, max_tokens=None, temperature=None, tools=None):
        self.calls += 1
        if self.reflexion_error:
            return ModelResponse(content="", model_id="stub", error=self.reflexion_error,
                                 input_tokens=10, output_tokens=0, duration_ms=10)
        return ModelResponse(content=json.dumps(self.lessons, ensure_ascii=False),
                             model_id="stub", input_tokens=10, output_tokens=50, duration_ms=10)


def _seed_audit_runs(temp_project: str, findings_by_seq: dict):
    """造单章任务（done）+ settled audit findings，模拟已写完章节的审计台账。"""
    from myink.models import AgentRun, Task

    with tenant_session(temp_project) as db:
        for seq, findings in findings_by_seq.items():
            tid = str(uuid.uuid4())
            # Task.id 必须 = AgentRun.task_id（章号映射 Task.id → chapter_seq 的关联键）
            db.add(Task(id=uuid.UUID(tid), project_id=uuid.UUID(temp_project),
                        task_type="chapter_generate", status="done", payload={"seq": seq},
                        chapter_seq=seq))
            db.add(AgentRun(project_id=uuid.UUID(temp_project), task_id=tid, node="audit",
                            role="Audit", model_id="stub", input_tokens=60, output_tokens=80,
                            duration_ms=30,
                            detail={"audit_verdict": {"verdict": "pass", "findings": findings}}))
        db.flush()


def test_chapter_window_reflexion_distills(temp_project, monkeypatch):
    """单章流窗口 [end-N+1, end]：audit findings → LLM 提炼 → 分级落库。"""
    _seed_audit_runs(temp_project, {
        1: [{"conflict_key": "p1", "conflict_type": "power", "severity": "major", "scope": "structural",
             "evidence": [{"chapter": 1, "quote": "林砚一掌拍出，直接晋升大境界"}],
             "confidence": 0.8, "suggestion": "突破须有契机"}],
        5: [{"conflict_key": "s1", "conflict_type": "style", "severity": "minor", "scope": "local",
             "evidence": [{"chapter": 5, "quote": "夜色沉沉，月色沉沉"}],
             "confidence": 0.7, "suggestion": "减少排比堆砌"}],
    })
    chain = WindowReflexionChain({"lessons": [
        {"conflict_type": "power", "lesson_type": "both", "content": "突破须有契机且配角战力受限",
         "confidence": 0.9},
        {"conflict_type": "style", "lesson_type": "writing", "content": "减少排比堆砌，对话区分腔调",
         "confidence": 0.8},
    ]})
    monkeypatch.setattr(nodes, "make_chain", lambda role, **kw: chain)

    out = nodes.reflexion_for_chapter_window(project_id=temp_project, end_chapter=5, task_id="r-w1")
    ref = out.get("reflexion") or {}
    assert ref.get("findings") == 2 and ref.get("lessons") == 2, f"实际 {ref}"
    assert chain.calls == 1

    with tenant_session(temp_project) as db:
        rows = db.query(WritingLesson).filter(
            WritingLesson.project_id == uuid.UUID(temp_project)).all()
        by_cat = {r.category: r for r in rows}
        assert by_cat["power"].status == "proposed" and by_cat["power"].source_chapter == 1
        assert by_cat["style"].status == "active" and by_cat["style"].source_chapter == 5
        runs = db.query(nodes.AgentRun).filter(nodes.AgentRun.task_id == "r-w1",
                                               nodes.AgentRun.node == "reflexion").all()
        assert len(runs) == 1, "应记一条 reflexion 运行记录"
        assert (runs[0].detail or {}).get("findings") == 2


def test_chapter_window_reflexion_no_findings_shortcircuits(temp_project, monkeypatch):
    """窗口内无发现 → 短路（不调 LLM），只记 no_new_findings 台账。"""
    _seed_audit_runs(temp_project, {3: []})
    chain = WindowReflexionChain()
    monkeypatch.setattr(nodes, "make_chain", lambda role, **kw: chain)

    out = nodes.reflexion_for_chapter_window(project_id=temp_project, end_chapter=5, task_id="r-w2")
    ref = out.get("reflexion") or {}
    assert ref.get("reason") == "no_new_findings" and ref.get("lessons") == 0, f"实际 {ref}"
    assert chain.calls == 0, "无发现应短路，不调 LLM"
    assert _count_lessons(temp_project) == 0


def test_chapter_window_includes_batch_subthreads(temp_project, monkeypatch):
    """批次子线程 task_id `{batch}:ch{seq}` 纳入窗口（章号从后缀解析，无需 Task 行）。"""
    from myink.models import AgentRun

    with tenant_session(temp_project) as db:
        db.add(AgentRun(project_id=uuid.UUID(temp_project), task_id="batch-x:ch3", node="audit",
                        role="Audit", model_id="stub", input_tokens=60, output_tokens=80,
                        duration_ms=30,
                        detail={"audit_verdict": {"verdict": "pass", "findings": [
                            {"conflict_key": "p1", "conflict_type": "power", "severity": "major",
                             "scope": "structural", "evidence": [{"chapter": 3, "quote": "越界"}],
                             "confidence": 0.8, "suggestion": "突破须有契机"}]}}))
        db.flush()
    chain = WindowReflexionChain({"lessons": [
        {"conflict_type": "power", "lesson_type": "both", "content": "突破须有契机",
         "confidence": 0.9}]})
    monkeypatch.setattr(nodes, "make_chain", lambda role, **kw: chain)

    out = nodes.reflexion_for_chapter_window(project_id=temp_project, end_chapter=5, task_id="r-w3")
    ref = out.get("reflexion") or {}
    assert ref.get("findings") == 1, f"批次子线程应纳入窗口，实际 {ref}"
    assert ref.get("lessons") == 1
    with tenant_session(temp_project) as db:
        row = db.query(WritingLesson).filter(
            WritingLesson.project_id == uuid.UUID(temp_project)).first()
        assert row.source_chapter == 3


def test_maybe_reflexion_interval_and_swallow(temp_project, monkeypatch):
    """worker 钩子：seq 命中间隔才触发；异常被吞（复盘不阻塞任务终态）。"""
    from myink.worker.processor import _maybe_reflexion

    calls = []

    def fake_reflexion(*, project_id, end_chapter, task_id):
        calls.append((end_chapter, task_id))

    monkeypatch.setattr(nodes, "reflexion_for_chapter_window", fake_reflexion)
    _maybe_reflexion(temp_project, 5, "t5")  # 5 % 5 == 0 → 触发
    _maybe_reflexion(temp_project, 4, "t4")  # 不命中
    _maybe_reflexion(temp_project, 0, "t0")  # seq<1 不触发
    assert calls == [(5, "t5")], f"实际 {calls}"

    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(nodes, "reflexion_for_chapter_window", boom)
    _maybe_reflexion(temp_project, 5, "t6")  # 不应抛异常
