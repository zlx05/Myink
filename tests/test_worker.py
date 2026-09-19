"""阶段 2 worker 单测（§6.12 幂等 / 物化 / 重试分类 / 取消跳过 / 租约锁）。

复用 test_flow 的活库 + 假 provider 模式；worker 直接消费队列消息跑 LangGraph。
验证（无真实 DeepSeek 依赖，redis 需起 myink-redis :6380）：
- process 物化：DB 无该 task_id → new_task 建行（status=running）→ 跑图 → 终态落库；
- 幂等去重：task 已终态（done/failed）→ 返回 "skip" 不重跑；
- 取消跳过：task=cancelled → 返回 "skip"，cancelled 不被终态回写覆盖；
- 重试分类：可重试异常（ConnectionError）→ 返回 "retry"（ZADD 由 consumer 负责）；
- 租约锁：活锁（他 worker 心跳新鲜）→ 返回 "skip"；僵尸锁（心跳过老/旧格式）→ 自动回收；
- 锁 CAS：释放不误删他人新锁；心跳续租保持 TTL；
- SSE：queue:sse:{task_id} 有 running / done 事件。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from myink.db import new_session, tenant_session
from myink.models import Project, Task
from myink.worker.processor import process
from myink.worker.redis_client import book_key, get_redis, inflight_key, lock_key, sse_key


def _owner(project_id):
    with new_session() as db:
        return str(db.get(Project, uuid.UUID(project_id)).user_id)


def _body(task_id, project_id, task_type="chapter_generate", payload=None, user_id=None):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "project_id": project_id,
        "user_id": user_id or _owner(project_id),
        "payload": payload or {},
        "trace_id": f"trace-{uuid.uuid4().hex[:8]}",
        "request_id": f"req-{uuid.uuid4().hex[:8]}",
        "retry_count": 0,
    }


def test_database_disconnects_are_retryable():
    """PG 重启/断连不能被判成不可重试，否则队列正常但任务会立即永久失败。"""
    from psycopg import InterfaceError, OperationalError
    from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

    from myink.worker.processor import _is_retryable

    assert _is_retryable(OperationalError("the connection is closed"))
    assert _is_retryable(InterfaceError("connection lost"))
    assert _is_retryable(SQLAlchemyOperationalError("select 1", {}, Exception("closed")))


def test_process_materialize_and_done(temp_project, stub_provider):
    """物化：DB 无 task_id → process 建行、跑图、done 落库；SSE 有 running/done 事件。

    用独立临时书（max_seq=0 → next=1），满足写保护（§11 顺序约束）且不污染 demo。
    """
    stub_provider("金丹", "金丹")
    project_id = temp_project
    task_id = str(uuid.uuid4())
    r = get_redis()
    body = _body(task_id, project_id, payload={"seq": 1})

    # 模拟网关已占本书并发闸门（gates.lua SADD，按书粒度 §13）→ 终态应释放
    r.sadd(inflight_key(body["user_id"], project_id), task_id)

    decision = process(body)

    assert decision == "terminal"
    with new_session() as db:
        row = db.get(Task, uuid.UUID(task_id))
        assert row is not None, "process 应物化任务行"
        assert row.status == "done", f"应 done，实际 {row.status}"
        assert row.trace_id == body["trace_id"], "trace_id 应透传"
        assert row.payload.get("seq") == 1

    # 并发闸门释放：inflight set 不含该任务
    assert not r.sismember(inflight_key(body["user_id"], project_id), task_id), "终态应 SREM 并发闸门"
    # lock 释放
    assert r.exists(lock_key(task_id)) == 0
    # SSE 事件：应有 running（物化阶段写）+ done（终态写）；fields 扁平存 event/status
    events = r.xrange(sse_key(task_id))
    types = [f["status"] for _, f in events if f.get("status")]
    assert "running" in types, f"SSE 应有 running，实际 {types}"
    assert types[-1] == "done", f"SSE 末事件应为 done，实际 {types}"

    # SSE 事件键 worker 直写只设 1h 过期，显式清理防残留（同 test_multiprocess._cleanup）
    r.delete(sse_key(task_id))


def test_worker_publishes_artifact_while_dispatch_is_still_running(temp_project, monkeypatch):
    """Worker 应在任务完成事件之前发布正文片段，而非结束后一次性补发。"""
    import myink.worker.processor as processor_mod
    from myink.workflow.streaming import ArtifactEmitter

    task_id = str(uuid.uuid4())
    body = _body(task_id, temp_project, payload={"seq": 1, "mode": "auto"})
    r = get_redis()
    observed_during_dispatch: list[dict[str, str]] = []

    def fake_dispatch(_body):
        emitter = ArtifactEmitter(
            task_id=task_id, chapter_seq=1, stage="write", attempt=1, chunk_size=4,
        )
        emitter.start()
        emitter.feed("模型仍在生成时到达的第一段")
        observed_during_dispatch.extend(fields for _, fields in r.xrange(sse_key(task_id)))
        emitter.complete()
        return {}

    monkeypatch.setattr(processor_mod, "_dispatch", fake_dispatch)

    try:
        assert process(body) == "terminal"
        live_deltas = [
            frame for frame in observed_during_dispatch
            if frame.get("event") == "artifact_delta" and frame.get("stage") == "write"
        ]
        assert live_deltas and live_deltas[0]["content"]
        assert not any(frame.get("status") == "done" for frame in observed_during_dispatch)

        frames = [fields for _, fields in r.xrange(sse_key(task_id))]
        live_deltas = [
            frame for frame in frames
            if frame.get("event") == "artifact_delta" and frame.get("stage") == "write"
        ]
        assert live_deltas
        assert frames[-1].get("status") == "done"
    finally:
        r.delete(sse_key(task_id), lock_key(task_id), book_key(temp_project))


def test_manual_plan_worker_waits_without_releasing_gate_then_resumes(temp_project, monkeypatch):
    """手动模式暂停时释放 worker/书锁但保留并发闸门；确认后同 task 断点完成并释放。"""
    import myink.providers as providers_mod
    from myink.models import AgentRun, Chapter
    from test_manual_plan import ManualPlanProvider

    provider = ManualPlanProvider()
    monkeypatch.setattr(providers_mod, "default_provider", provider)
    task_id = str(uuid.uuid4())
    r = get_redis()
    gate = inflight_key(_owner(temp_project), temp_project)
    body = _body(
        task_id, temp_project, payload={"seq": 1, "mode": "manual"},
    )
    r.sadd(gate, task_id)
    try:
        assert process(body) == "waiting"
        with tenant_session(temp_project) as db:
            task = db.get(Task, uuid.UUID(task_id))
            chapter = (db.query(Chapter)
                       .filter(Chapter.project_id == uuid.UUID(temp_project),
                               Chapter.chapter_seq == 1).one())
            plan_run = (db.query(AgentRun)
                        .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_chapter").one())
            approved = plan_run.detail["plan"]
            assert task.status == "awaiting_plan"
            assert task.payload["_gate_user_id"] == _owner(temp_project)
            assert chapter.status == "planning" and not chapter.content
        assert r.sismember(gate, task_id), "等待用户时应占住本书并发闸门"
        assert r.exists(lock_key(task_id)) == 0
        assert r.exists(book_key(temp_project)) == 0
        frames = [fields for _, fields in r.xrange(sse_key(task_id))]
        assert any(frame.get("event") == "artifact_complete" and frame.get("stage") == "plan"
                   for frame in frames)
        assert frames[-1]["status"] == "awaiting_plan"

        with new_session() as db:
            task = db.get(Task, uuid.UUID(task_id))
            task.status = "queued"
            task.payload = {**task.payload, "approved_plan": approved, "plan_attempt": 1}
            db.commit()
        resume = _body(
            task_id, temp_project, task_type="chapter_plan_resume",
            payload={"seq": 1, "mode": "manual", "approved_plan": approved, "plan_attempt": 1},
        )
        assert process(resume) == "terminal"
        with tenant_session(temp_project) as db:
            task = db.get(Task, uuid.UUID(task_id))
            chapter = (db.query(Chapter)
                       .filter(Chapter.project_id == uuid.UUID(temp_project),
                               Chapter.chapter_seq == 1).one())
            assert task.status == "done"
            assert chapter.status == "confirmed" and chapter.content
        assert not r.sismember(gate, task_id), "确认并完成后应释放并发闸门"
    finally:
        r.delete(gate, sse_key(task_id), lock_key(task_id), book_key(temp_project))


def test_stale_plan_resume_cannot_approve_a_new_replan_version(temp_project, monkeypatch):
    """第 1 版确认消息重放时，不得越过第 2 版 Plan 的人工确认点。"""
    import myink.providers as providers_mod
    from myink.models import AgentRun
    from test_manual_plan import ManualPlanProvider

    provider = ManualPlanProvider(replan_once=True)
    monkeypatch.setattr(providers_mod, "default_provider", provider)
    task_id = str(uuid.uuid4())
    r = get_redis()
    gate = inflight_key(_owner(temp_project), temp_project)
    first_body = _body(
        task_id, temp_project, payload={"seq": 1, "mode": "manual"},
    )
    r.sadd(gate, task_id)
    try:
        assert process(first_body) == "waiting"
        with new_session() as db:
            plan1 = (db.query(AgentRun)
                     .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_chapter")
                     .order_by(AgentRun.id.desc()).first()).detail["plan"]
            task = db.get(Task, uuid.UUID(task_id))
            task.status = "queued"
            db.commit()
        resume1 = _body(
            task_id, temp_project, task_type="chapter_plan_resume",
            payload={"seq": 1, "mode": "manual", "approved_plan": plan1, "plan_attempt": 1},
        )
        assert process(resume1) == "waiting", "审核 replan 后应再次等待人工"
        assert provider.plan_count == 2 and provider.calls.count("write") == 1

        # 模拟 RabbitMQ 把第 1 版确认消息再次交付。守卫应直接 ACK，不能执行第二次 write。
        assert process(resume1) == "waiting"
        assert provider.calls.count("write") == 1
        with new_session() as db:
            assert db.get(Task, uuid.UUID(task_id)).status == "awaiting_plan"
            plan2 = (db.query(AgentRun)
                     .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_chapter")
                     .order_by(AgentRun.id.desc()).first()).detail["plan"]
        assert r.sismember(gate, task_id), "丢弃旧确认消息时仍须保留闸门"

        with new_session() as db:
            task = db.get(Task, uuid.UUID(task_id))
            task.status = "queued"
            db.commit()
        resume2 = _body(
            task_id, temp_project, task_type="chapter_plan_resume",
            payload={"seq": 1, "mode": "manual", "approved_plan": plan2, "plan_attempt": 2},
        )
        assert process(resume2) == "terminal"
        assert provider.calls.count("write") == 2
        assert not r.sismember(gate, task_id)
    finally:
        r.delete(gate, sse_key(task_id), lock_key(task_id), book_key(temp_project))


def test_cancel_awaiting_plan_releases_gate_and_marks_empty_chapter(temp_project, monkeypatch):
    """用户不想采用 Plan 时可退出等待态，避免该书被永久锁住。"""
    import myink.providers as providers_mod
    from myink.api.routes_tasks import cancel_task
    from myink.models import Chapter
    from test_manual_plan import ManualPlanProvider

    monkeypatch.setattr(providers_mod, "default_provider", ManualPlanProvider())
    task_id = str(uuid.uuid4())
    r = get_redis()
    gate = inflight_key(_owner(temp_project), temp_project)
    body = _body(
        task_id, temp_project, payload={"seq": 1, "mode": "manual"},
    )
    r.sadd(gate, task_id)
    try:
        assert process(body) == "waiting"
        result = cancel_task(task_id)
        assert result["status"] == "cancelled"
        assert not r.sismember(gate, task_id)
        with tenant_session(temp_project) as db:
            task = db.get(Task, uuid.UUID(task_id))
            chapter = (db.query(Chapter)
                       .filter(Chapter.project_id == uuid.UUID(temp_project),
                               Chapter.chapter_seq == 1).one())
            assert task.status == "cancelled"
            assert chapter.status == "cancelled" and not chapter.content
    finally:
        r.delete(gate, sse_key(task_id), lock_key(task_id), book_key(temp_project))


def test_process_idempotent_skip_done(temp_project, stub_provider):
    """幂等：task 已 done → 返回 "skip" 不重跑（去重 §6.12）。"""
    stub_provider("金丹", "金丹")
    project_id = temp_project
    task_id = str(uuid.uuid4())
    r = get_redis()
    body = _body(task_id, project_id, payload={"seq": 1})

    assert process(body) == "terminal"  # 先跑一次到 done
    # 重复投递（dispatcher 重投 / 重复入队）：应 skip
    assert process(body) == "skip"
    # 不应产生第二条 done 事件覆盖（SSE 仍以首次 done 结尾，后续无事件）
    events = r.xrange(sse_key(task_id))
    types = [f["status"] for _, f in events if f.get("status")]
    assert types.count("done") == 1, f"done 事件只应 1 次，实际 {types}"

    # SSE 事件键 worker 直写只设 1h 过期，显式清理防残留（同 test_multiprocess._cleanup）
    r.delete(sse_key(task_id))


def test_process_cancel_skips_and_wins(project_id, stub_provider):
    """取消优先：task=cancelled → skip；runner 终态回写不覆盖 cancelled（§阶段2 守卫）。"""
    stub_provider("金丹", "金丹")
    from myink.workflow.runner import new_task

    task_id = new_task(
        project_id=project_id, task_type="chapter_generate",
        payload={"seq": 42}, chapter_seq=42, status="cancelled",
    )
    body = _body(task_id, project_id, payload={"seq": 42})
    assert process(body) == "skip", "已取消任务不应执行"
    with new_session() as db:
        assert db.get(Task, uuid.UUID(task_id)).status == "cancelled", "cancelled 应保持"


def test_process_batch_pause_publishes_paused(temp_project, monkeypatch):
    """在途批次手动暂停：process 跑批次 → 图中途查 DB paused 中断 → 任务保持 paused + SSE 末事件 paused。

    覆盖 processor._run 对 manual_halt 的 SSE 收尾（§6.12 批次控制）：暂停非终态——
    SSE 事件为 paused（非 done），流保持开启等 resume 续跑。用独立临时书（start=1）
    满足写序守卫且不污染 demo 数据。
    """
    import myink.providers as providers_mod
    import myink.workflow.batch_graph as bg_mod
    from myink.workflow.runner import new_task

    from test_flow import BatchHaltStub, _Chain

    tid = new_task(project_id=temp_project, task_type="batch_generate",
                   payload={"start": 1, "size": 3})

    def pause_batch():
        with new_session() as db:
            db.get(Task, uuid.UUID(tid)).status = "paused"
            db.commit()

    stub = BatchHaltStub("金丹", "金丹", on_write=2, halt_cb=pause_batch)
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))
    monkeypatch.setitem(providers_mod.DEFAULT_ROUTES, "writer", ["deepseek-v4-flash"])

    body = _body(tid, temp_project, task_type="batch_generate", payload={"start": 1, "size": 3})
    r = get_redis()
    try:
        assert process(body) == "terminal"
        with new_session() as db:
            assert db.get(Task, uuid.UUID(tid)).status == "paused", "暂停后任务应保持 paused"
        events = r.xrange(sse_key(tid))
        statuses = [f["status"] for _, f in events if f.get("status")]
        assert "paused" in statuses, f"SSE 应有 paused 事件，实际 {statuses}"
        assert statuses[-1] == "paused", f"SSE 末事件应为 paused（非 done），实际 {statuses}"
    finally:
        r.delete(sse_key(tid))


def test_process_resume_settles_terminal_status(temp_project, monkeypatch):
    """resume 终态落库（2026-08-17 修复）：resume_thread 不落库，process 结果分支补写。

    回归：chapter_resume 跑完后任务 DB 状态滞留 running——运行中无法续跑（409）、
    重启后也不可重投，任务永久卡死。mock _dispatch 返回 done / needs_review 结果，
    断言任务行终态被补写为 done / awaiting_review（awaiting_review 可再续跑）。
    """
    import myink.worker.processor as proc_mod
    from myink.workflow.runner import new_task

    r = get_redis()

    def run_done(*a, **k):
        return {"chapter_seq": 2}

    def run_review(*a, **k):
        return {"chapter_seq": 2, "needs_review": True}

    try:
        # 场景一：续跑成功 → done
        tid = new_task(project_id=temp_project, task_type="chapter_generate",
                       payload={"seq": 2}, chapter_seq=2, status="running")
        body = _body(tid, temp_project, task_type="chapter_resume", payload={"seq": 2})
        monkeypatch.setattr(proc_mod, "_dispatch", run_done)
        assert process(body) == "terminal"
        with new_session() as db:
            assert db.get(Task, uuid.UUID(tid)).status == "done", "续跑成功应补写 done"

        # 场景二：续跑转人工（persist has_card）→ awaiting_review，可再续跑
        tid2 = new_task(project_id=temp_project, task_type="chapter_generate",
                        payload={"seq": 2}, chapter_seq=2, status="running")
        body2 = _body(tid2, temp_project, task_type="chapter_resume", payload={"seq": 2})
        monkeypatch.setattr(proc_mod, "_dispatch", run_review)
        assert process(body2) == "terminal"
        with new_session() as db:
            assert db.get(Task, uuid.UUID(tid2)).status == "awaiting_review", \
                "续跑转人工应补写 awaiting_review"
    finally:
        r.delete(sse_key(tid)) if "tid" in locals() else None
        r.delete(sse_key(tid2)) if "tid2" in locals() else None


def test_process_retry_classification(project_id, stub_provider, monkeypatch):
    """可重试失败：dispatch 前置查询抛 ConnectionError → "retry"（§6.12 退避重投）。

    注意 LLM 层异常被 FallbackChain 吞成 resp.error（图上返回 error dict），
    不会逃逸到 _dispatch——retry 分类只拦截 dispatch 外的瞬态失败
    （PG/Redis 连接断），这正是 _resolve_chapter_seq 的位置。
    """
    stub_provider("金丹", "金丹")
    import myink.worker.processor as proc_mod

    def boom(*a, **k):
        raise ConnectionError("PG 连接瞬断")

    monkeypatch.setattr(proc_mod, "_resolve_chapter_seq", boom)
    task_id = str(uuid.uuid4())
    body = _body(task_id, project_id, payload={"seq": 43})

    try:
        decision = process(body)
        assert decision == "retry", f"可重试异常应返回 retry，实际 {decision}"
        # 重试不落 failed（等重投再跑）
        with new_session() as db:
            row = db.get(Task, uuid.UUID(task_id))
            assert row.status != "failed", "可重试失败不应置 failed"
    finally:
        # 自清理物化残留（2026-08-09）：retry 时任务行已是 running（等重投续跑，
        # 生产正确行为），但测试若不自删，每次 pytest 都在 demo 库积一条合成
        # running 行——污染演示数据。测试收尾删掉自己物化的行 + SSE 事件键。
        with new_session() as db:
            row = db.get(Task, uuid.UUID(task_id))
            if row is not None:
                db.delete(row)
                db.commit()
        get_redis().delete(sse_key(task_id))


def test_process_batch_write_order_guard(temp_project, stub_provider):
    """复查 B1：批次绕过章节顺序保护已修——start 跳过/为 0 → 任务 failed、不耗 LLM。

    单章有 _guard_write_order（只能写 max_seq+1），批次原本没有：batch_generate
    直接用 start 算各章 seq（batch_graph c['seq']=start+i），网关还允许 start 缺省 0
    → 可从第 0 章写起 / 跳过已有章，破坏 recall 连续性。修后 worker _dispatch 对批次
    首章同样 _guard_write_order（权威校验，网关只语法层 start>=1）。
    临时书 max_seq=0 → 合法首章 = 1；start=0（写第 0 章）与 start=2（跳过第 1 章）都应被拦。
    """
    stub_provider("金丹", "金丹")
    from myink.models import AgentRun

    project_id = temp_project
    r = get_redis()
    for bad_start in (0, 2):
        task_id = str(uuid.uuid4())
        body = _body(task_id, project_id, task_type="batch_generate",
                     payload={"size": 2, "start": bad_start})
        try:
            assert process(body) == "terminal", f"start={bad_start} 应终态（failed）"
            with new_session() as db:
                row = db.get(Task, uuid.UUID(task_id))
                assert row is not None, "process 应物化任务行"
                assert row.status == "failed", f"start={bad_start} 应 failed，实际 {row.status}"
                assert "顺序" in (row.error or ""), f"错误应说明章节顺序，实际 {row.error}"
                runs = db.query(AgentRun).filter(AgentRun.task_id == task_id).count()
                assert runs == 0, f"start={bad_start} 不应耗 LLM（无 agent_runs），实际 {runs}"
        finally:
            # 自清理：tasks 行 temp_project 夹具不删（只删 AgentRun/ProjectSettings/Project），
            # 显式删自己物化的行 + SSE 事件键
            with new_session() as db:
                row = db.get(Task, uuid.UUID(task_id))
                if row is not None:
                    db.delete(row)
                    db.commit()
            r.delete(sse_key(task_id))


def _live_lock_value(worker_id="other-worker") -> str:
    """构造他 worker 正在跑的活锁值（心跳新鲜，§6.12 租约锁格式）。"""
    return json.dumps({"token": "other-token", "worker_id": worker_id,
                       "heartbeat_at": int(time.time() * 1000)}, ensure_ascii=False)


def test_process_lock_prevents_dup(project_id, stub_provider):
    """租约锁防重：活锁（他 worker 心跳新鲜）→ 返回 "skip" 不执行。"""
    stub_provider("金丹", "金丹")
    task_id = str(uuid.uuid4())
    r = get_redis()
    r.set(lock_key(task_id), _live_lock_value(), nx=True, ex=60)
    body = _body(task_id, project_id, payload={"seq": 44})

    try:
        assert process(body) == "skip", "活锁被占应跳过"
        with new_session() as db:
            assert db.get(Task, uuid.UUID(task_id)) is None, "被锁任务不应物化"
    finally:
        r.delete(lock_key(task_id))


def test_process_lock_reclaims_zombie(temp_project, stub_provider):
    """崩溃恢复缺口修复（§6.12）：僵尸锁（心跳过老）→ process 自动回收并执行，不滞留。"""
    stub_provider("金丹", "金丹")
    project_id = temp_project
    task_id = str(uuid.uuid4())
    r = get_redis()
    # 心跳在 3×15s 之前停止（模拟 worker 崩溃后残留的僵尸锁）
    stale = json.dumps({"token": "zombie", "worker_id": "dead-worker",
                        "heartbeat_at": int(time.time() * 1000) - 100_000}, ensure_ascii=False)
    r.set(lock_key(task_id), stale, nx=True, ex=3600)
    body = _body(task_id, project_id, payload={"seq": 1})

    try:
        decision = process(body)
        assert decision == "terminal", f"僵尸锁应被回收并执行，实际 {decision}"
        with new_session() as db:
            assert db.get(Task, uuid.UUID(task_id)).status == "done", "僵尸锁回收后应跑完"
    finally:
        r.delete(lock_key(task_id), sse_key(task_id))


def test_process_lock_reclaims_legacy_format(temp_project, stub_provider):
    """迁移兼容：旧格式裸 worker-xxx 锁（阶段 2 升级前崩溃残留）→ 判僵尸可回收。"""
    stub_provider("金丹", "金丹")
    project_id = temp_project
    task_id = str(uuid.uuid4())
    r = get_redis()
    r.set(lock_key(task_id), "old-worker-string", nx=True, ex=3600)  # 旧版本无 JSON 锁值
    body = _body(task_id, project_id, payload={"seq": 1})

    try:
        decision = process(body)
        assert decision == "terminal", f"旧格式锁应判僵尸并回收执行，实际 {decision}"
    finally:
        r.delete(lock_key(task_id), sse_key(task_id))


def test_lock_release_does_not_delete_other_owner(project_id, stub_provider):
    """锁释放 CAS：持锁期间锁被他人接管（换 token）→ 释放不误删新锁。"""
    stub_provider("金丹", "金丹")
    from myink.worker.lock import TaskLock

    task_id = str(uuid.uuid4())
    r = get_redis()
    body = _body(task_id, project_id, payload={"seq": 44})

    try:
        # 模拟竞争：A 拿锁后，锁值被新 token 覆盖（他人接管）
        r.set(lock_key(task_id), _live_lock_value(), nx=True, ex=60)
        lock = TaskLock.acquire(r, lock_key(task_id), "worker-a")  # 会失败（活锁）→ None
        assert lock is None, "活锁被占应返回 None"
        assert r.exists(lock_key(task_id)) == 1, "活锁不应被误删"
    finally:
        r.delete(lock_key(task_id))


def test_lock_heartbeat_renews_ttl(monkeypatch):
    """锁心跳续租：持锁期间后台线程刷新心跳 + TTL，锁不因长任务而过期。"""
    import types

    from myink.worker.lock import TaskLock

    # 把锁模块的心跳间隔压到 0.2s，避免等 15s（settings 是 frozen dataclass，patch 模块引用即可）
    monkeypatch.setattr("myink.worker.lock.settings", types.SimpleNamespace(
        worker_lock_heartbeat=0.2, worker_inflight_ttl=60))

    task_id = str(uuid.uuid4())
    r = get_redis()
    lock = TaskLock.acquire(r, lock_key(task_id), "worker-hb")
    assert lock is not None, "应成功获取锁"
    try:
        time.sleep(0.6)  # 让后台线程续租 2-3 次
        cur = json.loads(r.get(lock_key(task_id)))
        hb_age = int(time.time() * 1000) - cur["heartbeat_at"]
        assert hb_age < 1000, f"续租应持续刷新心跳，当前滞后 {hb_age}ms"
        ttl = r.ttl(lock_key(task_id))
        assert 0 < ttl <= 60, f"续租应重置 TTL（约 60s），实际 {ttl}"
    finally:
        lock.release()
        assert r.exists(lock_key(task_id)) == 0, "release 应删除锁"


# ── 多书并行（§13 BYOK）：书锁同书串行、异书并行 ──────────────────────────────


def test_lock_release_reclaims_stale_value_same_worker(project_id):
    """E2E 暴露的释放竞态：renew 已应用到服务端但客户端 self._value 过期，
    CAS_DEL 值比对失配 → 锁滞留。release 应靠 worker_id 归属兜底删除，不滞到 TTL。"""
    from myink.worker.lock import TaskLock, _refresh_heartbeat

    r = get_redis()
    key = book_key(project_id)
    try:
        lock = TaskLock.acquire(r, key, "worker-stale-val")
        assert lock is not None
        cur = r.get(key)
        # 模拟：服务端已被本 worker 续租（值变了：新心跳，worker_id 不变），客户端没同步
        renewed = _refresh_heartbeat(cur)
        assert json.loads(renewed)["worker_id"] == "worker-stale-val", "续租不改归属"
        r.set(key, renewed, ex=60)
        lock.release()  # 值已失配，应靠 worker_id 兜底删除
        assert r.exists(key) == 0, "release 应删除仍归属本 worker 的过期值锁"
    finally:
        r.delete(key)


def test_lock_release_keeps_taken_over_lock(project_id):
    """释放不误删：锁被他人接管（worker_id 不同）→ release 不删除他人新锁。"""
    from myink.worker.lock import TaskLock

    r = get_redis()
    key = book_key(project_id)
    try:
        lock = TaskLock.acquire(r, key, "worker-a")
        assert lock is not None
        # 他人接管：整个值替换（worker_id 变）
        r.set(key, _live_lock_value("worker-b"), ex=60)
        lock.release()
        assert r.exists(key) == 1, "他人新锁不应被删除"
    finally:
        r.delete(key)


def test_book_lock_hetero_book_parallel(project_id):
    """异书并行核心：不同项目书锁互不阻塞；同书书锁互斥。

    书锁（lock:book:{pid}）是 worker 侧权威：网关并发闸门拦「同书第 2 个入队」，
    resume 直发绕闸门时靠书锁保证同书串行，异书在任何时刻都能并行。
    """
    from myink.worker.lock import TaskLock

    r = get_redis()
    lock_a = TaskLock.acquire(r, book_key(project_id), "worker-a")
    lock_b = TaskLock.acquire(r, book_key("another-project"), "worker-b")
    assert lock_a is not None and lock_b is not None, "异书书锁应各自可获取（并行）"
    try:
        dup = TaskLock.acquire(r, book_key(project_id), "worker-c")
        assert dup is None, "同书书锁应互斥"
    finally:
        lock_a.release()
        lock_b.release()
        assert r.exists(book_key(project_id)) == 0, "release 应删除书锁"
        assert r.exists(book_key("another-project")) == 0, "异书书锁也应删除"


def test_book_lock_release_cas(project_id):
    """书锁释放 CAS：持锁期间锁被他人接管（换 token）→ release 不误删新锁。"""
    from myink.worker.lock import TaskLock

    r = get_redis()
    key = book_key(project_id)
    try:
        lock = TaskLock.acquire(r, key, "worker-a")
        assert lock is not None
        r.set(key, _live_lock_value("usurper"), ex=60)  # 他人接管（书锁易主）
        lock.release()
        assert r.exists(key) == 1, "release 不应误删他人新书锁"
    finally:
        r.delete(key)


def test_process_book_lock_defers_same_book(project_id, stub_provider):
    """同书被占 → process 返回 "defer"（不物化、不执行，固定退避重投）。"""
    stub_provider("金丹", "金丹")
    task_id = str(uuid.uuid4())
    r = get_redis()
    r.set(book_key(project_id), _live_lock_value(), nx=True, ex=60)  # 他 worker 在写同书
    body = _body(task_id, project_id, payload={"seq": 45})

    try:
        assert process(body) == "defer", "同书书锁被占应 defer"
        with new_session() as db:
            assert db.get(Task, uuid.UUID(task_id)) is None, "defer 任务不应物化"
    finally:
        r.delete(book_key(project_id))


def test_process_book_lock_released_on_done(temp_project, stub_provider):
    """任务终态后书锁释放：同书下一任务可接棒（多书并行下同书串行不漏）。"""
    stub_provider("金丹", "金丹")
    project_id = temp_project
    task_id = str(uuid.uuid4())
    r = get_redis()
    body = _body(task_id, project_id, payload={"seq": 1})

    assert process(body) == "terminal"
    assert r.exists(book_key(project_id)) == 0, "任务终态应释放书锁"
    # 同书下一任务可立即获得书锁执行
    from myink.worker.lock import TaskLock

    next_lock = TaskLock.acquire(r, book_key(project_id), "worker-next")
    assert next_lock is not None, "书锁释放后同书下一任务可执行"
    next_lock.release()
    # SSE 事件键 worker 直写只设 1h 过期，显式清理防残留
    r.delete(sse_key(task_id))


def test_process_stable_worker_id_for_locks(temp_project, stub_provider, monkeypatch):
    """锁归属名稳定（2026-08-09 修复）：同进程连续两次 process 用同一 host-pid 归属名，
    release 的 worker_id 归属兜底（§难点22 释放竞态）才可靠——随机 uuid 会让归属失去辨识意义。"""
    stub_provider("金丹", "金丹")
    project_id = temp_project
    import myink.worker.processor as proc_mod

    from myink.worker.lock import TaskLock

    captured = []
    orig = TaskLock.acquire

    def spy(r, key, worker_id, ttl=None):
        captured.append(worker_id)
        return orig(r, key, worker_id, ttl)

    monkeypatch.setattr(TaskLock, "acquire", staticmethod(spy))
    task_ids = [str(uuid.uuid4()) for _ in range(2)]  # 同书两个任务先后跑（书锁释放后接棒）
    try:
        for seq, tid in zip((1, 2), task_ids):  # 写保护：临时书连写 1、2 章（合法顺序）
            body = _body(tid, project_id, payload={"seq": seq})
            assert process(body) == "terminal"
    finally:
        # SSE 事件键 worker 直写只设 1h 过期，显式清理防残留
        get_redis().delete(*(sse_key(tid) for tid in task_ids))

    # 两次 process 共 4 次 acquire（各 book + task），归属名应全部相同且带 worker- 前缀
    assert len(captured) >= 2, f"应捕获至少 2 次 acquire，实际 {len(captured)}"
    assert len(set(captured)) == 1, f"同进程锁归属名应一致，实际 {captured}"
    assert captured[0].startswith("worker-"), captured[0]


# ── 显式重写写序守卫（§7.3 失效重建触发点）──────────────────────────────────


def _seed_chapter(project_id: str, seq: int, *, status: str = "confirmed") -> None:
    """给临时书直接物化一章（绕图，测 guard 纯逻辑；temp_project 收尾 FK 级联删）。"""
    from myink.db import tenant_session
    from myink.models import Chapter

    with tenant_session(project_id) as db:
        db.add(Chapter(project_id=uuid.UUID(project_id), chapter_seq=seq,
                       title=f"第{seq}章", content="正文", status=status, version=1))
        db.commit()


def test_guard_rewrite_allows_confirmed(temp_project):
    """rewrite=True 放行已 confirmed 章（唯一合法重写对象）。"""
    from myink.worker.processor import _guard_write_order

    _seed_chapter(temp_project, 1)
    _guard_write_order(temp_project, 1, rewrite=True)  # 不放行会抛 WriteOrderError


def test_guard_rewrite_rejects_missing_or_beyond(temp_project):
    """rewrite=True 拒绝不存在的章 / 超过已写范围（空书重写第 3 章、只写到 1 重写 5）。"""
    from myink.worker.processor import WriteOrderError, _guard_write_order

    with pytest.raises(WriteOrderError):
        _guard_write_order(temp_project, 3, rewrite=True)
    _seed_chapter(temp_project, 1)
    with pytest.raises(WriteOrderError):
        _guard_write_order(temp_project, 5, rewrite=True)


def test_guard_rewrite_rejects_awaiting_review(temp_project):
    """rewrite=True 拒绝 awaiting_review 章（§6.11 确认分流：走 resume/reject 处理）。"""
    from myink.worker.processor import WriteOrderError, _guard_write_order

    _seed_chapter(temp_project, 1, status="awaiting_review")
    with pytest.raises(WriteOrderError):
        _guard_write_order(temp_project, 1, rewrite=True)


def test_guard_no_rewrite_stays_strict(temp_project):
    """无 rewrite 时旧严格校验不变：重写已写章仍拒绝、只放行 max_seq+1（默认参数零回归）。"""
    from myink.worker.processor import WriteOrderError, _guard_write_order

    _seed_chapter(temp_project, 1)
    with pytest.raises(WriteOrderError):
        _guard_write_order(temp_project, 1)  # 未带 rewrite 不能重写已写章
    _guard_write_order(temp_project, 2)       # max_seq+1 正常放行


def test_guard_resumes_empty_placeholder_without_skipping_chapter(temp_project):
    """生成中/失败的空章节仍是当前目标：允许重试本章，禁止直接跳到下一章。"""
    from myink.db import tenant_session
    from myink.models import Chapter
    from myink.worker.processor import WriteOrderError, _guard_write_order

    with tenant_session(temp_project) as db:
        db.add(Chapter(
            project_id=uuid.UUID(temp_project),
            chapter_seq=1,
            status="writing",
            generation_source="auto",
            version=1,
        ))

    _guard_write_order(temp_project, 1)
    with pytest.raises(WriteOrderError):
        _guard_write_order(temp_project, 2)


def test_dispatch_materializes_target_chapter_before_writing(temp_project, monkeypatch):
    """正文工作流启动前即建立目标章节页，流转不再寄生在上一章。"""
    import myink.worker.processor as processor
    from myink.db import tenant_session
    from myink.memory import repository as repo

    observed = {}

    def fake_generate_chapter(**kwargs):
        with tenant_session(temp_project) as db:
            chapter = repo.get_chapter(db, uuid.UUID(temp_project), 1)
            observed["chapter"] = chapter
            assert chapter is not None
            assert chapter.status == "writing"
        return {"persisted": False}

    monkeypatch.setattr(processor, "generate_chapter", fake_generate_chapter)
    result = processor._dispatch({
        "project_id": temp_project,
        "task_id": str(uuid.uuid4()),
        "task_type": "chapter_generate",
        "payload": {"seq": 1},
    })

    assert result == {"persisted": False}
    assert observed["chapter"].chapter_seq == 1


def test_batch_runner_materializes_each_chapter_before_its_subgraph(temp_project):
    """批次推进到后续章时也先建立该章页面，再运行该章子图。"""
    from myink.db import tenant_session
    from myink.memory import repository as repo
    from myink.workflow.batch_graph import make_chapter_runner

    class FakeChapterGraph:
        def invoke(self, state, config):
            with tenant_session(temp_project) as db:
                chapter = repo.get_chapter(db, uuid.UUID(temp_project), state["chapter_seq"])
                assert chapter is not None
                assert chapter.status == "writing"
            return {"persisted": False, "shared_context": {}}

    run_chapter = make_chapter_runner(FakeChapterGraph())
    result = run_chapter({
        "project_id": temp_project,
        "batch_task_id": str(uuid.uuid4()),
        "size": 3,
        "position": 1,
        "start_chapter": 1,
        "batch_plan": {"chapters": [
            {"seq": 1, "goal": "一"},
            {"seq": 2, "goal": "二"},
            {"seq": 3, "goal": "三"},
        ]},
    })

    assert result["position"] == 1
    with tenant_session(temp_project) as db:
        assert repo.get_chapter(db, uuid.UUID(temp_project), 2).status == "writing"
