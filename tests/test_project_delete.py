"""整本书删除（阶段 6 硬删）：409 守卫 + FK 级联清全部业务表/向量 + checkpoint/Redis 残留。

- 级联全清：章/事件+向量/池候选/势力/任务/agent_runs 一并删除，Redis 残留键（sse/lock）
  清空，Project 行不存在；
- 守卫 409：只拦 status=running（真有 worker 在跑）与书级租约锁存活（paused 但 worker 仍在
  写当前章，batch_graph 只在章边界停）；queued/paused/awaiting_plan/awaiting_review 属历史
  残留，删除时自动置 cancelled 后整本删掉（awaiting_review 在 UI 里没有取消入口，拦下会让
  整本书永远删不掉）；Redis inflight 键不参与守卫（泄漏会长期锁死，awaiting_review 下
  worker 也不释放它）；
- 404/403 矩阵（仿 test_auth.py 归属断言口径）；
- delete_threads：真实 PostgresSaver.put/put_writes 落 checkpoint 行 + 直接插 blob 行 +
  `:ch%` 批次前缀行 → 三表全清（batch run id 形如 {batch_id}:ch{seq}）。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection

from myink.api.main import app
from myink.api.routes_book import delete_project
from myink.config import settings
from myink.db import new_session, tenant_session
from myink.memory.vector_store import PgvectorStore
from myink.models import (AgentRun, Chapter, EmbeddingRow, Event, Faction,
                          MemoryCandidate, Project, Task, User)
from myink.worker.redis_client import (book_key, get_redis, inflight_key,
                                       lock_key, sse_key)
from myink.workflow.checkpointer import build_checkpointer, delete_threads

client = TestClient(app)

ZERO_VEC = [0.0] * 1024


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `myink init`（demo 用户未建）"
        return u.id


def _h(uid) -> dict:
    return {"X-Myink-User": str(uid)} if uid is not None else {}


def _seed_chapter(pid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        db.add(Chapter(project_id=uuid.UUID(pid), chapter_seq=seq, title=f"第{seq}章",
                       content=f"第{seq}章正文", status="confirmed", generation_source="auto"))
        db.commit()


def _seed_event_with_vec(pid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        ev = Event(project_id=uuid.UUID(pid), summary=f"事件{seq}", source_chapter=seq, confidence=0.9)
        db.add(ev)
        db.flush()
        PgvectorStore().upsert(db, project_id=uuid.UUID(pid), level="event", source_id=ev.id,
                               source_chapter=seq, model_version="bge-m3", embedding=ZERO_VEC)
        db.commit()


def _seed_pool_candidate(pid: str, seq: int) -> None:
    with tenant_session(pid) as db:
        db.add(MemoryCandidate(project_id=uuid.UUID(pid), kind="event", source_chapter=seq,
                               payload={"summary": f"候选{seq}", "participants": []}, confidence=0.8))
        db.commit()


def _seed_faction(pid: str) -> None:
    with tenant_session(pid) as db:
        db.add(Faction(project_id=uuid.UUID(pid), name="测试势力", stance="中立"))
        db.commit()


def _seed_task(pid: str, *, status: str = "done") -> str:
    """插一条任务（观测表无 RLS，new_session 直写），返回 task_id。"""
    with new_session() as db:
        t = Task(project_id=uuid.UUID(pid), task_type="chapter_generate", status=status,
                 payload={}, chapter_seq=1)
        db.add(t)
        db.commit()
        return str(t.id)


def _seed_agent_run(pid: str, task_id: str) -> None:
    with new_session() as db:
        db.add(AgentRun(project_id=uuid.UUID(pid), task_id=task_id, node="write", cost_est=0.01))
        db.commit()


def _counts(pid: str) -> tuple[int, ...]:
    """各业务表残留计数（都应为 0 才叫级联全清）。"""
    with new_session() as db:
        t = db.query(Task).filter(Task.project_id == uuid.UUID(pid)).count()
        a = db.query(AgentRun).filter(AgentRun.project_id == uuid.UUID(pid)).count()
        proj = db.get(Project, uuid.UUID(pid))
    with tenant_session(pid) as db:
        ch = db.query(Chapter).count()
        ev = db.query(Event).count()
        emb = db.query(EmbeddingRow).count()
        cand = db.query(MemoryCandidate).count()
        fac = db.query(Faction).count()
    return (proj is not None, ch, ev, emb, cand, fac, t, a)


# ---- 级联全清 ----


def test_delete_project_cascades_everything(temp_project):
    """终态任务下整书删除：章/事件+向量/池候选/势力/任务/agent_runs 全清 + Redis 残留键清空。"""
    pid = temp_project
    tid = _seed_task(pid, status="done")
    _seed_chapter(pid, 1)
    _seed_event_with_vec(pid, 1)
    _seed_pool_candidate(pid, 1)
    _seed_faction(pid)
    _seed_agent_run(pid, tid)
    uid = str(_demo_user_id())
    # Redis 残留：sse / lock 键（书锁存活是 409 判据、inflight 存在是闸门占用，见下方守卫用例）
    r = get_redis()
    r.set(sse_key(tid), "x")
    r.set(lock_key(tid), "x")
    try:
        result = delete_project(pid)
        assert result == {"project_id": pid, "deleted": True}
        assert _counts(pid) == (False, 0, 0, 0, 0, 0, 0, 0)
        assert r.exists(sse_key(tid)) == 0
        assert r.exists(lock_key(tid)) == 0
        assert r.exists(inflight_key(uid, pid)) == 0, "inflight 键应被清理（无任务时本就不存在）"
    finally:
        # 直接函数调用不经过 require_owner，Redis 键自清；防断言失败残留
        r.delete(book_key(pid), sse_key(tid), lock_key(tid), inflight_key(uid, pid))


# ---- 409 守卫（只拦真在跑的 running）----


def test_delete_project_refuses_running_task(temp_project):
    """worker 真在跑（status=running）→ 409，书与任务行都保持原状。"""
    _seed_task(temp_project, status="running")
    with pytest.raises(HTTPException) as ei:
        delete_project(temp_project)
    assert ei.value.status_code == 409
    with new_session() as db:
        assert db.get(Project, uuid.UUID(temp_project)) is not None, "409 守卫下不得删除"
        assert db.query(Task).filter(
            Task.project_id == uuid.UUID(temp_project), Task.status == "running").count() == 1


@pytest.mark.parametrize("status", ["queued", "paused", "awaiting_plan", "awaiting_review"])
def test_delete_project_cancels_non_running_tasks(temp_project, status):
    """非终态残留（queued/paused/awaiting_plan/awaiting_review）不再拦删除：置 cancelled 后删掉。"""
    _seed_task(temp_project, status=status)
    assert delete_project(temp_project) == {"project_id": temp_project, "deleted": True}
    with new_session() as db:
        assert db.get(Project, uuid.UUID(temp_project)) is None


def test_delete_project_ignores_leftover_inflight_key(temp_project):
    """Redis inflight 键残留（TTL 3600s 内未释放）不应锁死删除；删书顺带清掉该键。"""
    uid = str(_demo_user_id())
    key = inflight_key(uid, temp_project)
    r = get_redis()
    r.set(key, "1")
    try:
        assert delete_project(temp_project) == {"project_id": temp_project, "deleted": True}
        assert r.exists(key) == 0
    finally:
        r.delete(key)


def test_delete_project_refuses_while_worker_holds_book_lock(temp_project):
    """paused 但 worker 仍在写当前章（书级租约锁存活）→ 409。

    pause 只改 DB 状态，batch_graph 到章边界才停；这段时间删库会让 worker 白跑一章、
    落库撞 FK。书锁心跳续租、TTL 60s，崩溃自过期 → 不会像 inflight 键那样长期锁死删除。
    """
    r = get_redis()
    r.set(book_key(temp_project), "1", ex=60)
    try:
        with pytest.raises(HTTPException) as ei:
            delete_project(temp_project)
        assert ei.value.status_code == 409
        with new_session() as db:
            assert db.get(Project, uuid.UUID(temp_project)) is not None, "书锁守卫下不得删除"
    finally:
        r.delete(book_key(temp_project))


def test_delete_project_survives_redis_failure(temp_project, monkeypatch):
    """Redis 不可用时删书仍须完成：残留清理是 best-effort。

    否则会留下最坏状态——任务已 commit 成 cancelled（cancelled 不可续跑）、书还在，
    用户既删不掉也无法 resume，只能手工修库。
    """
    from myink.api import routes_book

    class _Boom:
        def exists(self, *args, **kwargs):
            raise RuntimeError("redis down")

        def delete(self, *args, **kwargs):
            raise RuntimeError("redis down")

    monkeypatch.setattr(routes_book, "get_redis", lambda: _Boom())
    assert delete_project(temp_project) == {"project_id": temp_project, "deleted": True}
    with new_session() as db:
        assert db.get(Project, uuid.UUID(temp_project)) is None


# ---- 404 / 403 矩阵（require_owner 挂依赖，走 TestClient HTTP 层）----


def test_delete_project_missing_404():
    resp = client.delete(f"/internal/v1/projects/{uuid.uuid4()}", headers=_h(_demo_user_id()))
    assert resp.status_code == 404


def test_delete_project_rejects_foreign_user(temp_project):
    resp = client.delete(f"/internal/v1/projects/{temp_project}", headers=_h(uuid.uuid4()))
    assert resp.status_code == 403


def test_delete_project_fail_closed_without_identity(temp_project):
    resp = client.delete(f"/internal/v1/projects/{temp_project}")
    assert resp.status_code == 403


def test_delete_project_rejects_invalid_identity(temp_project):
    resp = client.delete(f"/internal/v1/projects/{temp_project}", headers=_h("not-a-uuid"))
    assert resp.status_code == 403


# ---- delete_threads：清 checkpoint 三表（含 batch `:ch%` 前缀）----

_CONN_STR = settings.database_url.replace("postgresql+psycopg://", "postgresql://")


def _pg() -> Connection:
    return Connection.connect(_CONN_STR, autocommit=True)


def test_delete_threads_clears_checkpoint_tables():
    """真实 PostgresSaver.put/put_writes 落 checkpoints/writes 行；直接插 blob 行 + `:ch%`
    批次前缀行 → delete_threads 后三表全空（checkpoint 表无 FK，必须显式按 thread_id 清）。"""
    tid = f"del-cp-{uuid.uuid4().hex[:8]}"
    cid = f"1x{uuid.uuid4().hex}x3"
    config = {"configurable": {"thread_id": tid, "checkpoint_ns": "", "checkpoint_id": cid}}
    ckpt = Checkpoint(v=1, id=cid, ts="2026-08-17T00:00:00+00:00", channel_values={},
                      channel_versions={}, versions_seen={}, updated_channels=[])
    meta = CheckpointMetadata(source="input", step=1, writes=None, score=None)
    saver = build_checkpointer()  # 复用生产连接（put/put_writes 即真实运行写入路径）
    saver.put(config, ckpt, meta, {})
    saver.put_writes(config, [("channel", "value")], "task-1")
    # inline 存储模式不产 blob 行；直接插一行模拟 blob 存储模式，验证 SQL 三表全覆盖
    with _pg() as conn:
        conn.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (%s, '', 'channel', 1, 'json', %s)",
            (tid, b"{\"v\":1}"))
        # 批次每章 run id = {batch_id}:ch{seq} → LIKE {tid}:ch% 覆盖
        conn.execute(
            "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
            "VALUES (%s, '', '1xccccccccccccccccccccccccccccx3', 'batch-child', 0, 'c', 'json', %s)",
            (f"{tid}:ch1", b"{}"))
    try:
        delete_threads([tid])
        with _pg() as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                cur = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE thread_id = %s OR thread_id LIKE %s",
                    (tid, f"{tid}:ch%"))
                assert cur.fetchone()[0] == 0, f"{table} 应按 thread_id 清空（含 :ch% 前缀）"
    finally:
        delete_threads([tid])  # 幂等再清，防断言失败残留
