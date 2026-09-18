"""任务内部端点：查询（单条 + 项目历史列表）/ 批次控制（pause/resume/cancel）。

- 查询：tasks + agent_runs 无 RLS（观测/队列表），new_session 普通连接可查（§14）。
- `GET /projects/{pid}/tasks`：项目任务历史（全项目最近 50 条；按章查询完整历史）——前端
  选章后恢复过往任务，再走 `GET /tasks/{id}` 拿全部节点流转（agent_runs）。
- pause/resume/cancel（§6.12 暂停 vs 取消）：
  - pause    → status=paused（保留 Checkpointer 现场；真正节点级中断是阶段 3）
  - resume   → status in (failed/paused) → 置 queued + publish 一条 batch_resume 消息
               （同 task_id → thread_id 断点续跑，§6.12）
  - cancel   → status=cancelled（尽力而为，不回滚已落库；runner 终态守卫不回写）
- resume 的发布复用 worker 的 RabbitMQ 拓扑（myink.tasks key=tasks，见 worker/amqp.py），
  与网关入队同一主队列；延迟/死信由 RabbitMQ 侧承担，无网关 dispatcher。
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from myink.api.auth import require_owner
from myink.api.schemas import TaskControlOut, TaskDetailOut, TaskSummaryOut
from myink.config import settings
from myink.context_budget import estimate_tokens
from myink.db import new_session
from myink.models import AgentRun, Task
from myink.schemas import ChapterPlan
from myink.providers.base import effective_cost
from myink.providers.connections import price_tables_from_packed
from myink.worker import amqp
from myink.worker.redis_client import get_redis, inflight_key

router = APIRouter(prefix="/internal/v1", tags=["tasks"])

# resume 放行的前置状态（§6.12：失败续跑 / 暂停续跑 / critical 转人工后放行）
_RESUMABLE = {"failed", "paused", "queued", "awaiting_review"}


class PlanConfirmBody(BaseModel):
    plan: dict
    expected_attempt: int


def _task_uuid(raw: str) -> uuid.UUID:
    """非法 task_id → 400（评审 A4：pause/resume/cancel 统一不 500）。"""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"任务 id 非法: {raw}") from exc


def _project_price_tables(db, project_id: uuid.UUID) -> dict[str, dict[str, float]]:
    """账号环境单价覆盖书内遗留连接；按 model_id 回算历史 ¥0 行。"""
    from myink.environment import packed_models
    from myink.memory.repository import get_settings
    from myink.models import Project

    tables: dict[str, dict[str, float]] = {}
    st = get_settings(db, project_id)
    if st is not None:
        tables.update(price_tables_from_packed(st.model_routes))
    proj = db.get(Project, project_id)
    if proj is not None and proj.user_id is not None:
        tables.update(price_tables_from_packed(packed_models(proj.user_id)))
    return tables


def _run_cost(row: AgentRun, tables: dict[str, dict[str, float]] | None = None) -> float:
    extra = tables.get(row.model_id) if tables and row.model_id else None
    return effective_cost(
        cost_est=row.cost_est, model_id=row.model_id,
        input_tokens=row.input_tokens, output_tokens=row.output_tokens,
        cache_hit=row.cache_hit, prices=extra,
    )


def _row_cost(cost_est, model_id, input_tokens, output_tokens, cache_hit,
              tables: dict[str, dict[str, float]] | None = None) -> float:
    extra = tables.get(model_id) if tables and model_id else None
    return effective_cost(
        cost_est=cost_est, model_id=model_id,
        input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
        cache_hit=bool(cache_hit), prices=extra,
    )


def _batch_done_chapters(db, task_id: str) -> int:
    """批次已完成章数（§阶段2 派生口径）：该批 persist 节点去重章数——不能按任意
    agent_runs 计数（load_state 也写 run，章刚启动就被计为完成，评审 A7）。"""
    runs = db.query(AgentRun).filter(
        AgentRun.task_id.like(f"{task_id}:ch%"),
        AgentRun.node == "persist",
    ).all()
    return len({r.task_id for r in runs if (r.detail or {}).get("status") != "awaiting_review"})


def _task_payload(task_id: str, with_runs: bool = True) -> dict:
    """组装任务详情（status/payload/error/progress/完整 agent_runs）。"""
    with new_session() as db:
        task = db.get(Task, _task_uuid(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        data = {
            "task_id": str(task.id),
            "task_type": task.task_type,
            "status": task.status,
            "payload": {k: v for k, v in (task.payload or {}).items() if not k.startswith("_")},
            "error": task.error,
            "retry_count": task.retry_count,
            "trace_id": task.trace_id,
            "chapter_seq": task.chapter_seq,
            "batch_task_id": str(task.batch_task_id) if task.batch_task_id else None,
            "created_at": task.created_at.isoformat() if task.created_at else None,
        }
        # 批次进度 i/N 是派生量（§阶段2 状态机统一）：已完成章 = 该章 persist 节点执行过。
        # 不能按「任意 agent_runs」计数——load_state 也会写 run，章刚启动就被计为完成（评审 A7）。
        if task.task_type == "batch_generate" and task.payload:
            size = int(task.payload.get("size", 0))
            data["progress"] = {"current": _batch_done_chapters(db, task_id), "total": size}
        if with_runs:
            tables = _project_price_tables(db, task.project_id)
            runs = (
                db.query(AgentRun)
                .filter(AgentRun.task_id.like(f"{task_id}%"))
                .order_by(AgentRun.id)
                .all()
            )
            data["runs"] = [
                {
                    "task_id": r.task_id,
                    "node": r.node,
                    "model_id": r.model_id,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cache_hit": r.cache_hit,
                    "duration_ms": r.duration_ms,
                    "cost_est": _run_cost(r, tables),
                    "retry_count": r.retry_count,
                    "degraded": r.degraded,
                    "error": r.error,
                    "detail": r.detail,
                }
                for r in runs
            ]
            # 总花费（§6.8 成本透明，前端「每章总花费」）：单章 = 该章任务全部节点；
            # 批次 = 全批（task_id 前缀与 runs 同口径）。runs 已加载，求和免额外查询。
            data["cost_total"] = round(sum(item["cost_est"] for item in data["runs"]), 6)
        return data


@router.get("/tasks/{task_id}", response_model=TaskDetailOut)
def get_task(task_id: str) -> dict:
    return _task_payload(task_id)


@router.get("/projects/{project_id}/tasks",
            dependencies=[Depends(require_owner)], response_model=list[TaskSummaryOut])
def list_project_tasks(project_id: str, chapter_seq: int | None = None) -> list[dict]:
    """项目任务历史（倒序）：全项目最近 50 条；按章节查询时扫描完整历史。

    轻量摘要不含 runs（重量留给 GET /tasks/{id}）；批次进度派生（persist 去重章数）。
    require_owner 归属断言由依赖挂载（§14.1 ③）；tasks 无 RLS 观测表，new_session 可查。

    chapter_seq 非空（§右栏按章过滤）：只返回覆盖该章的任务——单章按 chapter_seq 精确
    匹配、批次按 payload.start..start+size 范围覆盖；cost_total 随之收窄为该章 agent_runs
    切片（批次 = {batch_id}:ch{seq} 行，单章 = 全任务；批次 book 级 run 如 batch_plan 不计入章成本）。
    """
    pid = _task_uuid(project_id)
    with new_session() as db:
        task_query = (
            db.query(Task)
            .filter(Task.project_id == pid)
            .order_by(Task.created_at.desc())
        )
        # 章节侧栏必须能找到旧章节任务；若先截项目最近 50 条，生成次数多后旧流程会消失。
        tasks = task_query.limit(50).all() if chapter_seq is None else task_query.all()
        # 每任务总花费（§6.8 成本透明）：agent_runs.cost_est 按 task_id 前缀聚合——
        # 单章任务 task_id=裸 uuid；批次任务每章 run= {batch_id}:ch{seq} + 裸 batch_id
        # （batch_plan/reflexion）。统一按「:」前段分桶，等价 _task_payload 的 LIKE 口径。
        # 一次查询取全部（项目级量级小，观测表无 RLS），避免逐任务子查询。
        tables = _project_price_tables(db, pid)
        runs = db.query(
            AgentRun.task_id, AgentRun.cost_est, AgentRun.model_id,
            AgentRun.input_tokens, AgentRun.output_tokens, AgentRun.cache_hit,
        ).filter(AgentRun.project_id == pid).all()
        if chapter_seq is None:
            # 全量口径：前缀分桶（批次 = 全批含 book 级 run；单章 = 全任务）
            cost_by_task: dict[str, float] = {}
            for tid, cost, model_id, inp, out, hit in runs:
                if not tid:
                    continue
                prefix = tid.split(":")[0]
                cost_by_task[prefix] = cost_by_task.get(prefix, 0.0) + _row_cost(
                    cost, model_id, inp, out, hit, tables)
        else:
            # 按章口径：批次只计 {batch_id}:ch{seq} 行；单章任务 = chapter_seq 命中时全任务
            batch_ch_cost: dict[str, float] = {}
            bare_cost: dict[str, float] = {}
            for tid, cost, model_id, inp, out, hit in runs:
                if not tid:
                    continue
                amount = _row_cost(cost, model_id, inp, out, hit, tables)
                if tid.count(":ch") == 1:
                    b, _, ch = tid.partition(":ch")
                    if ch.isdigit() and int(ch) == chapter_seq:
                        batch_ch_cost[b] = batch_ch_cost.get(b, 0.0) + amount
                else:
                    bare_cost[tid] = bare_cost.get(tid, 0.0) + amount
            cost_by_task = {}
            for t in tasks:
                if t.task_type == "batch_generate" and t.payload:
                    start = int(t.payload.get("start", 1))
                    size = int(t.payload.get("size", 0))
                    if start <= chapter_seq < start + size:
                        cost_by_task[str(t.id)] = batch_ch_cost.get(str(t.id), 0.0)
                elif t.chapter_seq == chapter_seq:
                    cost_by_task[str(t.id)] = bare_cost.get(str(t.id), 0.0)
        result = []
        for t in tasks:
            if chapter_seq is not None and str(t.id) not in cost_by_task:
                continue  # 不覆盖本章的任务过滤掉
            item: dict = {
                "task_id": str(t.id),
                "task_type": t.task_type,
                "status": t.status,
                "chapter_seq": t.chapter_seq,
                "batch_size": None,
                "batch_current": None,
                "cost_total": round(cost_by_task.get(str(t.id), 0.0), 6),
                "error": t.error,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            if t.task_type == "batch_generate" and t.payload:
                item["batch_size"] = int(t.payload.get("size", 0))
                item["batch_current"] = _batch_done_chapters(db, str(t.id))
            result.append(item)
        return result


@router.post("/tasks/{task_id}/pause", response_model=TaskControlOut)
def pause_task(task_id: str) -> dict:
    with new_session() as db:
        task = db.get(Task, _task_uuid(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status in ("done", "failed", "cancelled", "awaiting_plan"):
            raise HTTPException(status_code=409, detail=f"任务已终态，不可暂停: {task.status}")
        task.status = "paused"
        db.commit()
    return {"task_id": task_id, "status": "paused"}


@router.post("/tasks/{task_id}/resume", response_model=TaskControlOut)
def resume_task(task_id: str) -> dict:
    with new_session() as db:
        task = db.get(Task, _task_uuid(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status not in _RESUMABLE:
            raise HTTPException(status_code=409, detail=f"当前状态不可续跑: {task.status}")
        task.status = "queued"
        task.error = None
        payload = dict(task.payload)
        db.commit()
    # XADD 续跑消息（同 task_id → thread_id 断点续跑，§6.12）。
    # 按任务类型分流：批次 → batch_resume（batch 图整批续跑）；单章 → chapter_resume
    # （chapter 图续跑该章，critical 转人工后 resume 走此路径重跑放行）。
    resume_type = "batch_resume" if task.task_type == "batch_generate" else "chapter_resume"
    body = {
        "task_id": task_id,
        "task_type": resume_type,
        "project_id": str(task.project_id),
        "user_id": "",  # 阶段 3 归属断言补齐
        "payload": {**payload, "position": 0},
        "trace_id": task.trace_id or task_id,
        "request_id": task_id,
        "retry_count": 0,
        "created_at": "",
    }
    amqp.publish(json.dumps(body, ensure_ascii=False), amqp.KEY_TASKS)
    return {"task_id": task_id, "status": "queued", "message": "已投递续跑消息"}


@router.post("/tasks/{task_id}/plan/confirm", response_model=TaskControlOut)
def confirm_task_plan(task_id: str, body: PlanConfirmBody) -> dict:
    """确认手动模式计划，用同一任务 ID 从 plan_gate 断点继续。"""
    tid = _task_uuid(task_id)
    with new_session() as db:
        # 锁住任务行，避免双击/多标签页同时确认同一版计划并重复投递。
        task = db.get(Task, tid, with_for_update=True)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "awaiting_plan":
            raise HTTPException(status_code=409, detail=f"当前状态不可确认计划: {task.status}")
        payload = dict(task.payload or {})
        if task.task_type != "chapter_generate" or payload.get("mode") != "manual":
            raise HTTPException(status_code=409, detail="该任务不是手动单章写作")
        runs = (db.query(AgentRun)
                .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_chapter")
                .order_by(AgentRun.id.desc()).all())
        plan_run = next((run for run in runs if (run.detail or {}).get("plan")), None)
        if plan_run is None:
            raise HTTPException(status_code=409, detail="计划尚未生成完成")
        attempt = int((plan_run.detail or {}).get("plan_attempt") or 1)
        if body.expected_attempt != attempt:
            raise HTTPException(status_code=409, detail="PLAN_VERSION_CONFLICT")
        try:
            plan = ChapterPlan.model_validate(body.plan)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        plan.project_id = task.project_id
        plan.chapter_seq = int(payload.get("seq") or task.chapter_seq or 1)
        approved = plan.model_dump(mode="json")
        if estimate_tokens(approved) > min(16_000, settings.request_token_budget // 3):
            raise HTTPException(status_code=400, detail="PLAN_TOO_LARGE")
        # 用户可能编辑章节衔接锚点。恢复 worker 后才发现锚点无效会把任务打成
        # failed，因此在入队前用 checkpoint 中同一份召回上下文校验，直接返回可改错误。
        from myink.validation.continuity import check_transition_anchor
        from myink.workflow.runner import get_graphs

        chapter_graph, _ = get_graphs()
        snapshot = chapter_graph.get_state({"configurable": {"thread_id": task_id}})
        if not snapshot.values or not snapshot.values.get("plan"):
            raise HTTPException(status_code=409, detail="PLAN_CHECKPOINT_MISSING")
        try:
            check_transition_anchor(approved, snapshot.values.get("context") or {})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload["approved_plan"] = approved
        payload["plan_attempt"] = attempt
        task.payload = payload
        task.status = "queued"
        task.error = None
        project_id = str(task.project_id)
        trace_id = task.trace_id or task_id
        db.commit()

    message = {
        "task_id": task_id,
        "task_type": "chapter_plan_resume",
        "project_id": project_id,
        "user_id": payload.get("_gate_user_id") or "",
        "payload": {**payload, "approved_plan": approved},
        "trace_id": trace_id,
        "request_id": task_id,
        "retry_count": 0,
        "created_at": "",
    }
    try:
        amqp.publish(json.dumps(message, ensure_ascii=False), amqp.KEY_TASKS)
    except Exception as exc:
        with new_session() as db:
            current = db.get(Task, tid)
            if current and current.status == "queued":
                current.status = "awaiting_plan"
                db.commit()
        raise HTTPException(status_code=503, detail="计划确认投递失败，请重试") from exc
    return {"task_id": task_id, "status": "queued", "message": "计划已确认，开始写作"}


@router.post("/tasks/{task_id}/cancel", response_model=TaskControlOut)
def cancel_task(task_id: str) -> dict:
    with new_session() as db:
        task = db.get(Task, _task_uuid(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status in ("done", "cancelled"):
            raise HTTPException(status_code=409, detail=f"任务已终态: {task.status}")
        was_awaiting_plan = task.status == "awaiting_plan"
        payload = dict(task.payload or {})
        project_id = str(task.project_id)
        chapter_seq = int(payload.get("seq") or task.chapter_seq or 0)
        task.status = "cancelled"
        db.commit()
    if was_awaiting_plan:
        gate_user = payload.get("_gate_user_id") or ""
        if gate_user:
            get_redis().srem(inflight_key(gate_user, project_id), task_id)
        if chapter_seq:
            from myink.db import tenant_session
            from myink.models import Chapter

            with tenant_session(project_id) as db:
                chapter = (db.query(Chapter)
                           .filter(Chapter.project_id == uuid.UUID(project_id),
                                   Chapter.chapter_seq == chapter_seq).first())
                if chapter is not None and not (chapter.content or "").strip():
                    chapter.status = "cancelled"
    # 尽力而为：worker 启动前幂等检查跳过 / runner 终态守卫不回写（§6.12）
    return {"task_id": task_id, "status": "cancelled"}
