"""多进程并行自动化回归（§13 BYOK，2026-08-09）。

真正起 2 个 worker 进程消费同一 RabbitMQ 主队列，验证多书并行核心语义：
- 异书并行：两个不同 project 的任务同时投递 → 两个 worker 并发执行（overlap>0），都 done；
- 同书串行：同一 project 的两个任务同时投递 → 书锁保证一个执行、另一个 defer 退避重投，
  永不并发（overlap==0），最终都 done。

阶段 6 迁移 RabbitMQ 后：
- 入队 = amqp.publish 到 KEY_TASKS（-mp- 前缀隔离，不碰开发栈无前缀的真实队列）；
- 延迟退避 = RabbitMQ TTL+DLX 自动把 queue:delay 的到期消息弹回主队列，无需
  _DispatcherSim 模拟网关 dispatcher；
- QUEUE_PREFIX=-mp- 让本套件与开发栈 myink-worker 容器完全隔离（容器消费无前缀
  queue:tasks，本套件消费 queue:tasks-mp-）——不再需要先停容器（Redis 消费组会抢消息）。

配套 tests/_mp_worker.py（子进程入口：注入假 provider + 并发检测）。测试数据全用临时
project（复制 demo 的 Project+ProjectSettings），demo 零污染；清理 = 删临时 project
（FK 级联子表）+ agent_runs（无 FK 手动）+ Redis/RabbitMQ 残留。需活 Redis（myink-redis
:6380）+ 活 RabbitMQ（myink-rabbitmq :5672）+ 活 PG（myink init 建过 demo 项目）。
"""

from __future__ import annotations

import json
import os

# QUEUE_PREFIX 隔离点在 conftest.py（在 myink.config 冻结前设 -mp-）；此处重设仅作
# 防御性兜底（本模块被 pytest 加载时 settings 通常已被 conftest 初始化，实际不生效）。
os.environ["QUEUE_PREFIX"] = "-mp-"

import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import delete as sa_delete, select as sa_select  # noqa: E402

from myink.db import new_session  # noqa: E402
from myink.models import AgentRun, Project, ProjectSettings, Task  # noqa: E402
from myink.worker import amqp  # noqa: E402
from myink.worker.redis_client import book_key, get_redis, lock_key, sse_key  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MP_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mp_worker.py")


# ── worker 子进程管理 ─────────────────────────────────────────────────────────


def _purge_test_queues() -> None:
    """仅清理 -mp- 隔离队列；必须在 worker 启动前或完全停止后执行。"""
    with amqp.connect() as conn:
        ch = conn.channel()
        amqp.declare_topology(ch)
        for queue in (amqp.main_queue(), amqp.delay_queue(), amqp.dlq_queue()):
            ch.queue_purge(queue)


@pytest.fixture(scope="module")
def two_workers():
    """起 2 个真实 worker 进程（消费同一 RabbitMQ 主队列），等心跳就绪后 yield。"""
    r = get_redis()
    # 上次异常终止时，未确认消息会在 worker 退出后重新入队。启动前清理，避免消费引用
    # 已删除临时项目的旧消息；只操作 QUEUE_PREFIX=-mp- 的测试队列。
    _purge_test_queues()
    procs, logs = [], []
    for i in range(2):
        log_path = os.path.join(tempfile.gettempdir(), f"mp_worker_{i}.log")
        logf = open(log_path, "w", encoding="utf-8")
        p = subprocess.Popen(
            [sys.executable, _MP_WORKER],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=logf,
            env={**os.environ, "PYTHONPATH": ROOT},
        )
        procs.append(p)
        logs.append(logf)
    deadline = time.time() + 40
    while time.time() < deadline:
        own_heartbeats = {
            key for key in r.keys("queue:heartbeat:*")
            if any(key.endswith(f"-{process.pid}") for process in procs)
        }
        if len(own_heartbeats) == len(procs):
            break
        time.sleep(0.5)
    else:
        for p in procs:
            p.kill()
        for lf in logs:
            lf.close()
        pytest.fail("2 个 worker 未在 40s 内就绪，详见 tmp mp_worker_*.log")
    # 父进程先声明 -mp- 拓扑：worker 心跳线程先于其 declare_topology 启动，若父进程
    # 直接 publish 会打到不存在的交换机（404 NOT_FOUND）——三端幂等声明，重复声明安全。
    with amqp.connect() as conn:
        ch = conn.channel()
        amqp.declare_topology(ch)
    # 快照测试开始前的 SSE 键：拆卸时只清「测试期间新建」的键（worker 子进程读消息即写
    # queued 事件，可能在 _cleanup 之后补写 → 残留），绝不碰测试前就存在的真实通道。
    sse_before = set(r.keys("queue:sse:*"))
    yield procs
    for p in procs:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    for lf in logs:
        lf.close()
    # 先停 worker，再清理其退出时重新入队的未确认消息，保证下一轮测试从空队列开始。
    _purge_test_queues()
    own_heartbeats = {
        key for key in r.keys("queue:heartbeat:*")
        if any(key.endswith(f"-{process.pid}") for process in procs)
    }
    if own_heartbeats:
        r.delete(*own_heartbeats)
    for k in set(r.keys("queue:sse:*")) - sse_before:
        r.delete(k)


# ── 数据 helper ───────────────────────────────────────────────────────────────


def _demo_project_id() -> str:
    from sqlalchemy import text

    with new_session() as db:
        row = db.execute(text("SELECT id FROM projects WHERE title='九州问天'")).first()
        assert row is not None, "请先运行 myink init 建立 demo 项目"
        return str(row.id)


def _make_test_project(demo_id: str, tag: str) -> str:
    """复制 demo 的 Project + ProjectSettings 成独立临时书（demo 零污染，删书即清）。"""
    with new_session() as db:
        demo = db.get(Project, uuid.UUID(demo_id))
        b = Project(user_id=demo.user_id, title=f"MP回归书-{tag}", genre=demo.genre,
                    target_words=demo.target_words)
        db.add(b)
        db.flush()
        ds = db.execute(sa_select(ProjectSettings).where(
            ProjectSettings.project_id == demo.id)).scalar_one_or_none()
        if ds is not None:
            db.add(ProjectSettings(
                project_id=b.id, world_rules=ds.world_rules, style_profile=ds.style_profile,
                skill_pack=ds.skill_pack, genre_pack=getattr(ds, "genre_pack", None) or {},
                model_routes=ds.model_routes,
                hard_constraints=ds.hard_constraints, version=1))
        db.commit()
        return str(b.id)


def _enqueue(project_id: str, seq: int, tag: str) -> str:
    """publish 到 RabbitMQ 主队列（queue:tasks-mp-；网关未起，模拟入队结果），返回 task_id。"""
    task_id = str(uuid.uuid4())
    with new_session() as db:
        owner_id = str(db.get(Project, uuid.UUID(project_id)).user_id)
    body = {
        "task_id": task_id, "task_type": "chapter_generate",
        "project_id": project_id, "user_id": owner_id,
        "payload": {"seq": seq}, "trace_id": task_id, "request_id": task_id,
        "retry_count": 0,
    }
    amqp.publish(json.dumps(body, ensure_ascii=False), amqp.KEY_TASKS)
    return task_id


def _wait_status(task_id: str, timeout: int = 90) -> str:
    """轮询 tasks 表直到终态（worker 物化后落库）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with new_session() as db:
            row = db.get(Task, uuid.UUID(task_id))
            last = row.status if row is not None else None
            if row is not None and row.status in ("done", "failed", "cancelled"):
                if row.status == "failed":
                    raise AssertionError(f"task {task_id[:8]} 执行失败: {row.error}")
                return row.status
        time.sleep(0.5)
    raise AssertionError(f"task {task_id[:8]} 未在 {timeout}s 内终态，末态 {last}")


def _cleanup(project_ids: list[str], task_ids: list[str]) -> None:
    """删临时 project（级联子表）+ agent_runs（无 FK 手动）+ Redis/RabbitMQ 残留。"""
    with new_session() as db:
        for pid in project_ids:
            db.execute(sa_delete(AgentRun).where(AgentRun.project_id == uuid.UUID(pid)))
            db.execute(sa_delete(Project).where(Project.id == uuid.UUID(pid)))
        db.commit()
    r = get_redis()
    for tid in task_ids:
        r.delete(sse_key(tid), lock_key(tid))
    for pid in project_ids:
        r.delete(book_key(pid))
    # RabbitMQ 残留：清 -mp- 前缀持久队列（主/延迟/死信）。TTL 退避消息由 DLX 自动回弹，
    # 无 Redis ZSET/Stream 残留；清队列保证多次运行互不污染（在途已交付消息不受 purge 影响）。
    with amqp.connect() as conn:
        ch = conn.channel()
        for q in (amqp.main_queue(), amqp.delay_queue(), amqp.dlq_queue()):
            try:
                ch.queue_purge(q)
            except Exception:
                pass


# ── 测试 ──────────────────────────────────────────────────────────────────────


def test_hetero_book_parallel(two_workers):
    """异书并行：两本不同书的任务同时投递 → 两个 worker 并发执行（书锁互不阻塞）。"""
    r = get_redis()
    r.delete("mp:overlap", "mp:active")
    demo_id = _demo_project_id()
    book_a = _make_test_project(demo_id, "A")
    book_b = _make_test_project(demo_id, "B")
    ta = _enqueue(book_a, 1, "a")
    tb = _enqueue(book_b, 1, "b")
    try:
        assert _wait_status(ta) == "done"
        assert _wait_status(tb) == "done"
        overlap = int(r.get("mp:overlap") or 0)
        assert overlap > 0, "异书任务应由两个 worker 并发执行（书锁互不阻塞），overlap 应 >0"
    finally:
        _cleanup([book_a, book_b], [ta, tb])


def test_same_book_serial(two_workers):
    """同书串行：同一本书两个任务同时投递 → 书锁保证 defer→接棒，永不并发，最终都 done。

    defer 重投由 RabbitMQ TTL+DLX 自动弹回主队列（queue:delay 到期死信回 queue:tasks），
    无需模拟 dispatcher；书锁释放后重投消息接棒执行。
    """
    r = get_redis()
    r.delete("mp:overlap", "mp:active")
    demo_id = _demo_project_id()
    book = _make_test_project(demo_id, "S")
    t1 = _enqueue(book, 1, "s1")
    t2 = _enqueue(book, 2, "s2")
    try:
        assert _wait_status(t1) == "done"
        assert _wait_status(t2) == "done"
        overlap = int(r.get("mp:overlap") or 0)
        assert overlap == 0, "同书任务应串行（书锁保证 defer→接棒），overlap 应 ==0"
    finally:
        _cleanup([book], [t1, t2])
