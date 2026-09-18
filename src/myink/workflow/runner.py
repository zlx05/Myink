"""工作流入口：编译两张图 + 生成单章 / 批次任务 API（阶段 1 CLI/API 共用）。"""

from __future__ import annotations

import uuid

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from myink.db import tenant_session
from myink.workflow import nodes
from myink.workflow.batch_graph import build_batch_graph
from myink.workflow.chapter_graph import build_chapter_graph
from myink.workflow.checkpointer import build_checkpointer

_batch_graph: CompiledStateGraph | None = None
_chapter_graph: CompiledStateGraph | None = None


def get_graphs() -> tuple[CompiledStateGraph, CompiledStateGraph]:
    """惰性编译（checkpointer 只建一次）。返回 (chapter_graph, batch_graph)。"""
    global _batch_graph, _chapter_graph
    if _chapter_graph is None:
        cp = build_checkpointer()
        _chapter_graph = build_chapter_graph(checkpointer=cp)
        _batch_graph = build_batch_graph(_chapter_graph, checkpointer=cp)
    return _chapter_graph, _batch_graph


def generate_chapter(*, project_id: str, chapter_seq: int, task_id: str | None = None,
                     user_instruction: str | None = None, rewrite: bool = False,
                     writing_mode: str = "auto") -> dict:
    """生成单章（阶段 1 同步版；阶段 2 由 worker 消费 Redis 队列调用）。

    task_id 作 thread_id：中断/恢复/重试续跑同一条执行链（§6.7）。
    rewrite：显式重写已确认章（§7.3 失效重建）——persist 先失效该章旧记忆再写新。
    """
    chapter_graph, _ = get_graphs()
    thread_id = task_id or str(uuid.uuid4())
    result = chapter_graph.invoke(
        {
            "project_id": project_id,
            "chapter_seq": chapter_seq,
            "task_id": thread_id,
            "user_instruction": user_instruction,
            "rewrite": rewrite,
            "writing_mode": writing_mode,
        },
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 64},
    )
    _finish_chapter_result(project_id, thread_id, result)
    return result


def resume_chapter_plan(*, project_id: str, task_id: str, approved_plan: dict) -> dict:
    """从 plan_gate 的动态 interrupt 恢复；不重新调用 Planner。"""
    chapter_graph, _ = get_graphs()
    result = chapter_graph.invoke(
        Command(resume=approved_plan),
        config={"configurable": {"thread_id": task_id}, "recursion_limit": 64},
    )
    _finish_chapter_result(project_id, task_id, result)
    return result


def _finish_chapter_result(project_id: str, task_id: str, result: dict) -> str:
    if result.get("__interrupt__"):
        result["awaiting_plan"] = True
        _set_task_status(project_id, task_id, "awaiting_plan")
        return "awaiting_plan"
    if result.get("error"):
        _set_task_status(project_id, task_id, "failed", result["error"])
        return "failed"
    status = "awaiting_review" if result.get("needs_review") else "done"
    _set_task_status(project_id, task_id, status)
    return status


def generate_batch(*, project_id: str, size: int, start_chapter: int,
                   batch_task_id: str | None = None) -> dict:
    """自动写作批次（§6.11）。batch_task_id 作批次 thread_id，整批可续跑。

    单章失败 → BatchChapterError 中断（§6.12）：任务置 failed，batch 线程 checkpoint
    停在失败章；调用方用同一 batch_task_id + resume_thread 续跑，从失败章继续、
    不重跑已完成章。返回 {"batch_failed": True, "error": ...} 供调用方识别。
    """
    from myink.workflow.batch_graph import BatchChapterError, BatchHaltError, BatchReviewError

    _, batch_graph = get_graphs()
    thread_id = batch_task_id or str(uuid.uuid4())
    state = {
        "project_id": project_id,
        "batch_task_id": thread_id,
        "size": size,
        "position": 0,
        "start_chapter": start_chapter,
    }
    try:
        return batch_graph.invoke(state, config={"configurable": {"thread_id": thread_id}, "recursion_limit": 64})
    except BatchReviewError as exc:
        # critical 冲突转人工（§6.11）：任务置 awaiting_review，checkpoint 停在本章，
        # 人工确认候选后 resume 从本章续跑（不重跑已完成章）。
        _set_task_status(project_id, thread_id, "awaiting_review", str(exc))
        return {"batch_paused": True, "error": str(exc), "needs_review": True}
    except BatchHaltError as exc:
        # 手动暂停/取消（§6.12）：状态已由控制端点置 paused/cancelled，此处幂等确认
        # （cancelled 经 _set_task_status 守卫跳过，paused 补写）+ 返回标记供 processor
        # 发对应 SSE 事件。checkpoint 停在本章，resume 从断点续跑。
        _set_task_status(project_id, thread_id, exc.status)
        return {"manual_halt": exc.status}
    except BatchChapterError as exc:
        _set_task_status(project_id, thread_id, "failed", str(exc))
        return {"batch_failed": True, "error": str(exc)}


def resume_thread(graph: CompiledStateGraph, thread_id: str, state: dict) -> dict:
    """从断点续跑（§6.12：批次中断/服务重启/人工暂停后）。

    同一 thread_id 再 invoke：LangGraph 从最后 checkpoint 继续，不重跑已完成节点；
    失败章重试。续跑输入以 checkpoint 状态为基座（`{**state, **checkpoint}`，
    checkpoint 优先）——进度字段（如批次 position）不被外部输入覆盖，
    否则会冲回 0 重跑已完成章。外部 state 只补缺口（首次无 checkpoint 时兜底）。
    """
    snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    resume_state = {**state, **(snap.values or {})}  # checkpoint 优先
    return graph.invoke(resume_state, config={"configurable": {"thread_id": thread_id}, "recursion_limit": 64})


def finalize_chapter_review(*, project_id: str, task_id: str, chapter_seq: int) -> dict:
    """§6.11 确认流收尾：人工确认候选后，把 checkpoint 草稿直接落库为正文（不重跑整章）。

    修复（2026-08-17）：LangGraph 1.2.9 的 invoke-resume 语义是「整图从 START 重跑」，
    不是断点续跑。confirm→resume 若走 resume_thread 重跑整章，确定性输入会再次命中
    critical → 永久 awaiting_review、章节永远无法最终确认（用户实测：确认后点续跑变成重生成）。
    收尾取 checkpoint 状态（含 write 产出的草稿），掩码 report 走 node_persist auto 路径
    确认正文状态 + 推进进度；记忆已在确认流分头处理（confirmed 落库 / 未处理留池 / 新实体
    已建），不再重复写。
    """
    chapter_graph, _ = get_graphs()
    snap = chapter_graph.get_state({"configurable": {"thread_id": task_id}})
    if not snap.values or not snap.values.get("draft"):
        raise ValueError(f"任务 {task_id} 无草稿 checkpoint，无法确认流收尾")
    from myink.workflow.review import resolve_review
    result = resolve_review(chapter_graph, snap.values, task_id=task_id)
    # 落库后追加 LLM 真摘要（§7 短期记忆）：finalize 路径不走图，这里单独补一次。
    # node_summarize 自吞异常（失败保留启发式摘要），不阻塞确认流收尾。
    nodes.node_summarize(result)
    return result


def new_task(*, project_id: str, task_type: str, payload: dict, chapter_seq: int | None = None,
             task_id: str | None = None, trace_id: str | None = None,
             status: str = "queued") -> str:
    """创建任务（DB 为最终权威，§5.3/§6.12 幂等键）。

    阶段 2 worker 物化队列消息时传入 task_id（幂等键，与 Redis 消息同 id）、trace_id；
    默认行为与阶段 1 完全一致（自生成 uuid、status=queued）。
    """
    from myink.models import Task

    with tenant_session(project_id) as db:
        task = Task(id=uuid.UUID(task_id) if task_id else uuid.uuid4(),
                    project_id=uuid.UUID(project_id), task_type=task_type,
                    payload=payload, status=status, chapter_seq=chapter_seq,
                    trace_id=trace_id)
        db.add(task)
        db.flush()
        return str(task.id)


def _set_task_status(project_id: str, task_id: str, status: str, error: str | None = None) -> None:
    """更新任务终态（tasks 是观测/队列数据，但业务写入仍走租户会话保持一致性）。

    阶段 2 守卫：cancelled 优先——用户取消后（尽力而为，§6.12）不再被后续终态回写覆盖。
    """
    from myink.models import Task

    with tenant_session(project_id) as db:
        task = db.get(Task, uuid.UUID(task_id))
        if task and task.status != "cancelled":
            task.status = status
            if error:
                task.error = error
