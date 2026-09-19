"""单条任务消息的处理：幂等检查 → 物化 → running → dispatch → 终态/重试分类。

对 runner 的衔接：直接 import 调用 generate_chapter / generate_batch / resume_thread
（核心签名不变，§阶段2 明确不改）；终态落库复用 runner._set_task_status / 批次图。

返回值约定（供 consumer 决策）：
- "skip"     已处理过/取消/他 worker 在跑 → 直接 ACK 丢弃
- "terminal" 正常终态（done/awaiting_review/failed/paused 已由 runner 落库）
- "waiting"  手动模式等待计划确认；释放 Worker/书锁，但保留网关并发占位
- "retry"    可重试失败 → ACK + ZADD queue:delay 退避
- "defer"    同书被占（书锁活）→ ACK + ZADD queue:delay 固定退避重投（不计 retry_count，
             书忙是瞬态非失败，重投不进 DLQ；同书串行、异书并行由书锁保证，§13）
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import date

from psycopg import InterfaceError as PsycopgInterfaceError
from psycopg import OperationalError as PsycopgOperationalError
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from myink.config import settings
from myink.db import new_session
from myink.models import Project, Task
from myink.workflow.runner import (
    finalize_chapter_review,
    generate_batch,
    generate_chapter,
    get_graphs,
    new_task,
    resume_chapter_plan,
    resume_thread,
)
from myink.workflow.streaming import bind_artifact_sink
from myink.worker.lock import TaskLock
from myink.worker.redis_client import (
    book_key,
    cost_key,
    get_redis,
    inflight_key,
    lock_key,
    sse_key,
)

logger = logging.getLogger(__name__)

# 可重试失败：LLM 瞬态 / 限流 / 网络 / Redis / PG 瞬态（§6.12 调用层 + 任务层）
_RETRYABLE = (
    TimeoutError,
    ConnectionError,
    OSError,
    PsycopgInterfaceError,
    PsycopgOperationalError,
    SQLAlchemyOperationalError,
)


def _clamp_batch_size(raw: object) -> int:
    """批次 size 硬上限（§6.11 成本熔断第一道闸）：默认 5、最大 20，超上限 clamp。
    worker 侧双保险（网关已 clamp，防 payload 直投绕过）。"""
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = settings.batch_max_default
    return max(1, min(size, settings.batch_max_hard))


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, _RETRYABLE) or (
        hasattr(exc, "status_code") and getattr(exc, "status_code") in (429, 500, 502, 503, 504)
    )


def _resolve_chapter_seq(project_id: str, payload: dict) -> int:
    """单章任务从 payload 取 seq，缺省时查章节表当前进度（tenant_session 带租户上下文）。"""
    from myink.db import tenant_session

    seq = payload.get("seq")
    if seq is not None:
        return int(seq)
    from myink.models import Project

    with tenant_session(project_id) as db:
        proj = db.get(Project, uuid.UUID(project_id))
        return (proj.current_chapter + 1) if proj else 1


def _latest_plan_attempt(db, task_id: str) -> int:
    """读取当前待确认的业务计划版本；解析重试行没有 plan，会自动略过。"""
    from myink.models import AgentRun

    rows = (db.query(AgentRun)
            .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_chapter")
            .order_by(AgentRun.id.desc()).all())
    row = next((item for item in rows if (item.detail or {}).get("plan")), None)
    return int((row.detail or {}).get("plan_attempt") or 0) if row else 0


def _chapter_awaiting_review(project_id: str, seq: int) -> bool:
    """章节是否处于确认流暂停（§6.11 awaiting_review，待人工处理候选后 resume 放行）。"""
    from myink.db import tenant_session
    from myink.models import Chapter

    with tenant_session(project_id) as db:
        ch = db.query(Chapter).filter(
            Chapter.project_id == uuid.UUID(project_id),
            Chapter.chapter_seq == seq,
        ).first()
        return ch is not None and ch.status == "awaiting_review"


class WriteOrderError(Exception):
    """章节顺序校验失败（重写已写章节 / 跳章），worker 置任务 failed，不消耗 LLM。"""


def _guard_write_order(project_id: str, seq: int, *, rewrite: bool = False) -> None:
    """单章写保护：只允许写「已写最大章 + 1」（§11 章节顺序约束）。

    理由：跳章（写 1,2,4）会让中间章缺失，后续 recall 的"上一章"跳到更早章，上下文断裂；
    重写已写章会覆盖正文，但第 2 章内容基于旧第 1 章，覆盖后第 2 章不级联 → 剧情断层。
    写保护把这些状态从「允许但产出漂移」收敛为「显式拒绝 + 报错」，前端据此引导用户写下一章。

    rewrite=True（显式重写，§7.3 失效重建触发）：目标章必须已存在且 confirmed 才放行——
    await/review 章走 resume/reject，跳章/新章仍拒绝；批次首章仍走严格校验（写序连续性）。
    """
    from myink.db import tenant_session
    from myink.models import Chapter, Project
    from sqlalchemy import func

    with tenant_session(project_id) as db:
        proj = db.get(Project, uuid.UUID(project_id))
        if proj is None:
            return  # 项目不存在，让下游正常报错
        if proj.creation_status not in {"ready", "legacy_ready"}:
            raise ValueError("PROJECT_NOT_READY: 请先确认设定与整书大纲")
        max_seq = db.query(func.max(Chapter.chapter_seq)).filter(
            Chapter.project_id == proj.id).scalar()
        latest = None
        if max_seq is not None:
            latest = db.query(Chapter).filter(
                Chapter.project_id == proj.id,
                Chapter.chapter_seq == max_seq,
            ).first()
        # 生成任务会在真正写作前先建一条空的 writing 章节，供页面和流转绑定。
        # worker 重投或用户重试时必须继续写这同一章；同时不能把空占位误算成
        # “已完成一章”而放行下一章，否则会制造正文缺口。
        latest_is_placeholder = bool(
            latest is not None
            and not (latest.content or "").strip()
            and latest.status in ("planning", "writing", "failed", "cancelled")
        )
        expected = int(max_seq) if latest_is_placeholder else (max_seq or 0) + 1
        if rewrite:
            if seq > (max_seq or 0):
                raise WriteOrderError(
                    f"重写失败：第 {seq} 章不存在（已写到第 {max_seq or 0} 章）。"
                )
            ch = db.query(Chapter).filter(
                Chapter.project_id == proj.id, Chapter.chapter_seq == seq).first()
            if ch is None:
                raise WriteOrderError(f"重写失败：第 {seq} 章不存在。")
            if ch.status != "confirmed":
                raise WriteOrderError(
                    f"仅 confirmed 章节可重写；第 {seq} 章状态为 {ch.status}，请走 resume/reject 处理。"
                )
            return
        if seq != expected:
            raise WriteOrderError(
                f"章节顺序校验失败：只能写第 {expected} 章（已写到第 {max_seq or 0} 章），"
                f"收到第 {seq} 章。重写已写章节 / 跳章会破坏上下文连续性。"
            )


def _dispatch(body: dict) -> dict:
    """按 task_type 派发到 runner（同步阻塞跑完整任务）。"""
    project_id = body["project_id"]
    task_id = body["task_id"]
    task_type = body["task_type"]
    payload = body.get("payload") or {}

    if task_type == "chapter_generate":
        seq = _resolve_chapter_seq(project_id, payload)
        rewrite = bool(payload.get("rewrite"))
        _guard_write_order(project_id, seq, rewrite=rewrite)
        if not rewrite:
            from myink.db import tenant_session
            from myink.memory import repository as repo

            with tenant_session(project_id) as db:
                repo.ensure_chapter_placeholder(
                    db, project_id=uuid.UUID(project_id), chapter_seq=seq,
                    status="planning" if payload.get("mode") == "manual" else "writing",
                )
        return generate_chapter(
            project_id=project_id,
            chapter_seq=seq,
            task_id=task_id,
            user_instruction=payload.get("user_instruction"),
            rewrite=rewrite,
            writing_mode="manual" if payload.get("mode") == "manual" else "auto",
        )
    if task_type == "chapter_plan_resume":
        return resume_chapter_plan(
            project_id=project_id,
            task_id=task_id,
            approved_plan=payload["approved_plan"],
        )
    if task_type == "batch_generate":
        # 批次写序守卫（复查 B1）：与单章同口径——批次首章必须 = 已写最大章 + 1。
        # 网关只能语法校验 start>=1，此处是权威校验（start 跳过/覆盖已有章会破坏
        # recall 连续性，batch_graph 用 start 直接算各章 seq，无内置守卫）。
        start_chapter = int(payload.get("start", 1))
        _guard_write_order(project_id, start_chapter)
        from myink.db import tenant_session
        from myink.memory import repository as repo

        with tenant_session(project_id) as db:
            repo.ensure_chapter_placeholder(
                db, project_id=uuid.UUID(project_id), chapter_seq=start_chapter,
            )
        return generate_batch(
            project_id=project_id,
            size=_clamp_batch_size(payload.get("size", settings.batch_max_default)),
            start_chapter=start_chapter,
            batch_task_id=task_id,
        )
    if task_type == "batch_resume":
        chapter_graph, batch_graph = get_graphs()
        return resume_thread(
            batch_graph,
            task_id,
            {
                "project_id": project_id,
                "batch_task_id": task_id,
                "size": _clamp_batch_size(payload.get("size", settings.batch_max_default)),
                "start_chapter": int(payload.get("start", 1)),
                "position": int(payload.get("position", 0)),
            },
        )
    if task_type == "chapter_resume":
        # 单章续跑（§6.11 确认分流放行 / §6.12 失败续跑）：chapter 图以 task_id 作
        # thread_id 续跑该章；checkpoint 优先（resume_thread 语义），外部只补缺口。
        # rewrite 同透传：重写任务中断后续跑，persist 仍先失效旧记忆（checkpoint 也有，
        # 双保险）。批次续跑不传（批次无单章重写语义）。
        seq = int(payload.get("seq", 1))
        # 确认流收尾：章节 awaiting_review（首跑 critical/L2 major/新人物卡片暂停）→
        # 取 checkpoint 草稿直接落库正文，不走图重跑——LangGraph 1.2.9 resume 语义是
        # 整图从 START 重跑，确定性输入会再命中确认分流 → 永久 awaiting_review、正文
        # 永不落库（2026-08-17 用户实测：确认后点续跑变成重新生成）。
        if _chapter_awaiting_review(project_id, seq):
            return finalize_chapter_review(project_id=project_id, task_id=task_id, chapter_seq=seq)
        chapter_graph, _ = get_graphs()
        return resume_thread(
            chapter_graph,
            task_id,
            {
                "project_id": project_id,
                "task_id": task_id,
                "chapter_seq": seq,
                "user_instruction": payload.get("user_instruction"),
                "rewrite": bool(payload.get("rewrite")),
            },
        )
    raise ValueError(f"未知 task_type: {task_type}")


def _mark_empty_writing_chapters(body: dict, status: str) -> None:
    """任务终止时同步空占位状态，避免章节列表永久显示“写作中”。"""
    task_type = body.get("task_type")
    if task_type not in ("chapter_generate", "chapter_plan_resume", "batch_generate"):
        return
    payload = body.get("payload") or {}
    seq = int(payload.get("seq", 1)) if task_type != "batch_generate" \
        else int(payload.get("start", 1))

    from myink.db import tenant_session
    from myink.models import Chapter

    with tenant_session(body["project_id"]) as db:
        query = db.query(Chapter).filter(
            Chapter.project_id == uuid.UUID(body["project_id"]),
            Chapter.chapter_seq >= seq,
            Chapter.status.in_(("planning", "writing")),
        )
        if task_type != "batch_generate":
            query = query.filter(Chapter.chapter_seq == seq)
        for chapter in query.all():
            if not (chapter.content or "").strip():
                chapter.status = status


def _pub_status(task_id: str, status: str, extra: dict | None = None) -> None:
    """写 SSE 进度事件到 queue:sse:{task_id}（worker 侧唯一写 SSE 的地方）。"""
    r = get_redis()
    data = {"event": "status", "task_id": task_id, "status": status}
    if extra:
        data.update(extra)
    r.xadd(sse_key(task_id), data, maxlen=1000)
    r.expire(sse_key(task_id), 3600)


def _pub_artifact(root_task_id: str, event: dict[str, str]) -> None:
    """把工作流产物事件写入任务 SSE Stream；观测失败不影响正文生成。"""
    try:
        r = get_redis()
        r.xadd(sse_key(root_task_id), event, maxlen=1000)
        r.expire(sse_key(root_task_id), 3600)
    except Exception as exc:
        logger.warning("SSE 文本片段写入失败（不阻塞任务）: task=%s err=%s", root_task_id, exc)


def _release_inflight(body: dict) -> None:
    """释放并发闸门（同书放行新任务，§13 三层闸门）。幂等：SREM 不存在即无操作。

    调用时机（评审 A1）：终态 / skip / 最后一次重试（下站进 DLQ）才释放；
    retry 期间保持闸门关闭——否则同书新任务提前放行，写序守卫（§11）会把它
    拦成 WriteOrderError，重试任务反而被误杀。
    """
    r = get_redis()
    user_id = body.get("user_id")
    if user_id:
        r.srem(inflight_key(user_id, body["project_id"]), body["task_id"])


def _accumulate_cost(body: dict) -> None:
    """累计该任务实际成本到当日账单（§6.8 成本透明）。

    只在终态或最后一次重试时调用一次：agent_runs 已含历次尝试的全量行，
    一次求和即真实总成本；每次 retry 都累计会把同一批 agent_runs 重复计入
    （评审 A1）。观测累计失败不阻塞任务。
    """
    r = get_redis()
    try:
        from myink.models import AgentRun

        with new_session() as db:
            rows = db.query(AgentRun).filter(AgentRun.task_id.like(f"{body['task_id']}%")).all()
            total = sum(r.cost_est for r in rows)
        if total > 0:
            r.incrbyfloat(cost_key(date.today().isoformat()), total)
    except Exception:
        logger.warning("累计成本失败（可忽略）: task=%s", body["task_id"])


def _finish(body: dict, decision: str) -> None:
    """锁释放后的收尾（评审 A1：retry 语义修正）。

    - 并发闸门：retry 期间保持关闭（同书不提前放行新任务）；终态 / skip /
      最后一次重试才释放（最后一次重试后下站进 DLQ，不释放会永久锁死本书）。
    - 成本累计：只在终态或最后一次重试求和一次，避免 retry 重复计入。
    """
    final_attempt = int(body.get("retry_count") or 0) >= settings.worker_max_retries - 1
    if decision != "waiting" and (decision != "retry" or final_attempt):
        _release_inflight(body)
    if decision == "terminal" or (decision == "retry" and final_attempt):
        _accumulate_cost(body)


def _maybe_reflexion(project_id: str, chapter_seq: int, task_id: str) -> None:
    """单章流每 N 章复盘（§8.9 扩展）：失败仅记日志，不阻塞任务终态（SSE done 已发）。

    批次流走 batch_end node_reflexion，不走这里。复盘是加分项：LLM 失败/DB 异常都只
    warning，绝不把已完成的章节任务拉回失败。
    """
    if chapter_seq < 1 or chapter_seq % settings.chapter_reflexion_interval != 0:
        return
    from myink.workflow import nodes

    try:
        nodes.reflexion_for_chapter_window(project_id=project_id, end_chapter=chapter_seq,
                                           task_id=task_id)
    except Exception as exc:
        logger.warning("章节窗口复盘失败（不阻塞任务）: %s", exc)


def _run(body: dict, task_id: str, task_type: str, project_id: str) -> str:
    """派发并分类终态（decision = terminal / waiting / retry，SSE 在此写）。"""
    _pub_status(task_id, "running")
    try:
        with bind_artifact_sink(lambda event: _pub_artifact(task_id, event)):
            result = _dispatch(body)
    except Exception as exc:
        # 批次 critical 冲突：resume 续跑时由图内节点抛出（首次运行在 runner
        # generate_batch 内 catch 置 awaiting_review）。置 awaiting_review 等人工
        # 处理候选后 resume，不标 failed（§6.11 确认分流）。
        from myink.workflow.batch_graph import BatchHaltError, BatchReviewError

        if isinstance(exc, BatchReviewError):
            from myink.workflow.runner import _set_task_status

            _set_task_status(project_id, task_id, "awaiting_review", str(exc))
            logger.info("批次 critical 转人工暂停: %s err=%s", task_id, exc)
            _pub_status(task_id, "awaiting_review", {"error": str(exc)})
            return "terminal"
        if isinstance(exc, BatchHaltError):
            # 手动暂停/取消（§6.12 批次控制）：batch_resume 续跑时图内再次感知
            # paused/cancelled 中断（首次批次由 generate_batch 接住转 manual_halt 标记，
            # 不走到这里）。状态幂等确认 + 发对应 SSE 事件（cancelled 终态关流、
            # paused 非终态保持流开启等 resume 续跑）。
            from myink.workflow.runner import _set_task_status

            _set_task_status(project_id, task_id, exc.status)
            _pub_status(task_id, exc.status)
            return "terminal"
        if _is_retryable(exc):
            logger.warning("可重试失败，将退避重投: %s err=%s", task_id, exc)
            return "retry"
        # 不可重试 → 置 failed（runner 未覆盖的异常路径）
        from myink.workflow.runner import _set_task_status

        _mark_empty_writing_chapters(body, "failed")
        _set_task_status(project_id, task_id, "failed", str(exc))
        logger.error("不可重试失败: %s err=%s", task_id, exc)
        return "terminal"

    # 终态落库 + SSE。首跑已由 runner 内部 _set_task_status（generate_chapter/batch），
    # resume 路径（§6.12：chapter_resume/batch_resume 走 resume_thread）runner 不落库，
    # 此处统一补写终态——否则 resume 完成后任务永久滞留 running（DB 为权威，重启后
    # 无法续跑也不可重投，2026-08-17 修复）。_set_task_status 幂等且 cancelled 优先
    # 守卫，对首跑重复设置无害。
    # 先判手动暂停/取消（§6.12：generate_batch 返回 manual_halt 标记）再判转人工
    # （critical 冲突同时带 error 说明）再判失败，保证 SSE 状态正确。
    from myink.workflow.runner import _set_task_status

    if result.get("manual_halt"):
        if result["manual_halt"] == "cancelled":
            _mark_empty_writing_chapters(body, "cancelled")
        _set_task_status(project_id, task_id, result["manual_halt"])
        _pub_status(task_id, result["manual_halt"])
    elif result.get("awaiting_plan"):
        _set_task_status(project_id, task_id, "awaiting_plan")
        _pub_status(task_id, "awaiting_plan")
        return "waiting"
    elif result.get("needs_review"):
        _set_task_status(project_id, task_id, "awaiting_review")
        _pub_status(task_id, "awaiting_review")
    elif result.get("error"):
        logger.warning("任务失败（runner 已落 failed）: %s err=%s", task_id, result["error"])
        _mark_empty_writing_chapters(body, "failed")
        _set_task_status(project_id, task_id, "failed", result["error"])
        _pub_status(task_id, "failed", {"error": result["error"]})
    else:
        _set_task_status(project_id, task_id, "done")
        _pub_status(task_id, "done")
        # 单章流每 N 章复盘：SSE done 先发、复盘后跑（不延迟用户感知；批次流走 batch_end）
        if task_type in ("chapter_generate", "chapter_plan_resume", "chapter_resume"):
            _maybe_reflexion(project_id, int((body.get("payload") or {}).get("seq", 0)), task_id)
    return "terminal"


def valid_task_owner(body: dict) -> bool:
    """Reject mismatched message identities before locks, SSE, or DB writes."""
    try:
        project_id = uuid.UUID(str(body.get("project_id")))
        user_id = uuid.UUID(str(body.get("user_id")))
        task_id = uuid.UUID(str(body.get("task_id")))
    except (ValueError, TypeError, AttributeError):
        return False
    with new_session() as db:
        project = db.get(Project, project_id)
        if project is None or project.user_id != user_id:
            return False
        task = db.get(Task, task_id)
        return task is None or task.project_id == project_id


def process(body: dict, worker_id: str | None = None) -> str:
    """处理单条消息，返回 consumer 决策。body 为已 decode 的消息字段 dict。"""
    if not valid_task_owner(body):
        logger.warning("拒绝任务归属不匹配的消息: %s", body.get("task_id"))
        return "skip"
    r = get_redis()
    task_id = body["task_id"]
    task_type = body["task_type"]
    project_id = body["project_id"]
    # 锁归属名必须进程内稳定（2026-08-09 修复）：原每次随机 uuid 会让 release 的
    # worker_id 归属兜底（§难点22 释放竞态）失去意义——同进程两把锁归属名不同，
    # 排查/兜底都不可靠。改 host-pid：同进程所有任务同归属名、跨进程天然唯一；
    # consumer 传自己的 worker_id（与心跳名一致）时以此为准。
    worker_self = worker_id or f"worker-{socket.gethostname()}-{os.getpid()}"

    # 书锁（§13 BYOK 多书并行）——同书串行、异书并行，先于任务锁获取（固定顺序防死锁）。
    # 网关并发闸门只拦「同书第 2 个入队」；resume 直发（routes_tasks 绕闸门）与
    # 崩溃重投竞态时，书锁是权威：他 worker 在写同书 → defer 退避重投（不计 retry_count）。
    book = TaskLock.acquire(r, book_key(project_id), worker_self)
    if book is None:
        logger.info("书忙退避（他 worker 在写同书）: %s book=%s", task_id, project_id)
        return "defer"

    # 幂等 ①：任务租约锁（§6.12 崩溃恢复缺口修复）——跨 worker 防重 + 崩溃自回收。
    # 持锁期间后台续租心跳，崩溃后锁在 TTL(60s) 内自过期 / 被僵尸判定回收，
    # 网关认领重投后新 worker 能拿到锁执行（不再「他 worker 在跑」滞留）。
    lock = TaskLock.acquire(r, lock_key(task_id), worker_self)
    if lock is None:
        book.release()
        logger.info("跳过（他 worker 在跑）: %s", task_id)
        return "skip"

    decision = "skip"
    run_it = False
    try:
        # 幂等 ②：查 DB 现状（tasks 无 RLS，普通连接可查）
        with new_session() as db:
            row = db.get(Task, uuid.UUID(task_id))
            if row and row.status == "awaiting_plan" and task_type == "chapter_plan_resume" \
                    and int((body.get("payload") or {}).get("plan_attempt") or 0) != _latest_plan_attempt(db, task_id):
                # RabbitMQ 至少一次投递可能重放上一版确认消息。若此时审核已 replan 到
                # 新版，旧 Command(resume) 绝不能越过第二次人工确认；ACK 丢弃旧消息，
                # 同时沿用 waiting 决策保留并发闸门。
                decision = "waiting"
                logger.info("跳过过期计划确认: %s message_attempt=%s current_attempt=%s",
                            task_id, (body.get("payload") or {}).get("plan_attempt"),
                            _latest_plan_attempt(db, task_id))
            elif row and row.status in ("done", "failed", "cancelled", "awaiting_review", "awaiting_plan") \
                    and not (row.status == "awaiting_plan" and task_type == "chapter_plan_resume"):
                logger.info("跳过（已终态，去重）: %s status=%s", task_id, row.status)
            elif row and row.status == "paused" and task_type != "batch_resume":
                logger.info("跳过（已暂停，等 resume）: %s", task_id)
            elif row is None:
                # 物化：DB 为最终权威（§5.3），幂等键 task_id 作 PK
                new_task(
                    project_id=project_id,
                    task_type=task_type,
                    payload={**(body.get("payload") or {}),
                             "_gate_user_id": body.get("user_id") or ""},
                    chapter_seq=(body.get("payload") or {}).get("seq"),
                    task_id=task_id,
                    trace_id=body.get("trace_id"),
                    status="running",
                )
                logger.info("物化新任务: %s (%s)", task_id, task_type)
                run_it = True
            else:
                # queued → running（worker 写，§阶段2）
                row.status = "running"
                db.commit()
                run_it = True

        if run_it:
            decision = _run(body, task_id, task_type, project_id)

    finally:
        lock.release()  # CAS 删除：仅当锁仍归自己，不误删他人新锁（§6.12 修复）
        book.release()  # 书锁同样 CAS 删除，释放后他 worker 可接棒同书任务
        _finish(body, decision)
    return decision
