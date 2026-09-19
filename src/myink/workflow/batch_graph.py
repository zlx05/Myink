"""批次主图（spec/state-flow.md §1 外层循环 + plan.md §6.11 自动写作批次）。

batch_plan（Planner 一次产出 N 章推进蓝图，N 是规划单元不是重复次数）
→ 单章子图 ×N（每章后状态桥：上章 persist 沉淀 → 下章 recall 连续推进）
→ route_after_chapter：
   - 本章 audit 判 replan(batch) → replan_batch（回 batch_plan 重规划剩余章，§6.11）
   - critical 未解决 → batch_paused（暂停批次等人工，§6.11 已确认）
   - 单章失败（重试+降级后） → 抛 BatchChapterError 中断批次（不走到 END，见下方续跑说明）
   - 还有下章 → next_chapter（状态桥 → 再跑子图）
   - 批次数到 N → batch_end（批次汇总）

批次级共享上下文（§6.11 共享池）：recall 稳定部分（hard_facts）批次内首章组装、
桥回 batch state，后续章复用——省重复组装 + 章间版本一致。

续跑（§6.12 任务层）：batch 图 thread_id = batch_task_id；每章子图用派生
thread（batch_task_id:ch{seq}）。章失败抛 BatchChapterError → batch 线程
checkpoint 停在 chapter 节点（position 未推进、图未达 END），任务置 failed；
同 thread 再 invoke（resume_thread）从失败章续跑，已完成章不重跑，成功后
batch_end 将任务置 done。单章子图 thread 因失败已走完（优雅 error → END），
续跑时该章从开头重跑，批次级「不重跑已完成章」不受影响。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph

from myink.config import settings
from myink.db import tenant_session
from myink.memory import repository as repo
from myink.providers import make_chain
from myink.validation import global_audit as ga
from myink.workflow import nodes, prompts
from myink.workflow.outline import normalize_outline
from myink.workflow.state import BatchState, ChapterState

logger = logging.getLogger(__name__)


class BatchChapterError(Exception):
    """单章失败中断批次（spec/state-flow.md §5：从失败章续跑）。

    设计：批次图遇到章失败（优雅 error 或子图异常）**抛异常而非返回状态**——
    LangGraph 只在图未达 END 时支持同 thread 再 invoke 从断点续跑；若让批次
    优雅走到 batch_end，图已 END，续跑会从头重跑整个批次。抛异常使 batch 线程
    checkpoint 停在 chapter 节点（position 未推进），续跑从失败章继续、不重跑
    已完成章。任务状态由 runner 的 generate_batch catch 后置 failed。
    """


class BatchReviewError(Exception):
    """批次 critical 冲突转人工（§6.11 确认分流：暂停批次等人工）。

    与 BatchChapterError 同一续跑机制：抛异常使 checkpoint 停在本章、resume 从
    本章续跑不重跑已完成章；区别在终态语义——任务置 awaiting_review（而非 failed），
    人工确认候选后 resume 放行。若走 batch_paused→batch_end→END 的优雅路径，
    续跑只能整批重跑，违背"不重跑已完成章"。
    """


class BatchHaltError(Exception):
    """手动暂停/取消中断批次（§6.12 批次控制）。

    用户在途中点暂停/取消 → 控制端点只改 DB 任务状态（paused/cancelled），
    图在章节边界查 DB 感知并中断批次。与 BatchReviewError 同一续跑机制：抛异常
    使 checkpoint 停在本章（position 未推进）、resume 从本章续跑不重跑已完成章。
    若走优雅路径到 batch_end（图 END），resume 只能整批重跑，违背"不重跑已完成章"。
    status 取任务状态（paused/cancelled），runner/processor 据此落终态 + 发 SSE。
    """

    def __init__(self, status: str):
        super().__init__(f"批次已{status}")
        self.status = status


_BATCH_PLAN_PROMPT = """你是长篇网文创作系统的【批次规划 Agent】。为接下来 N 章做整体推进蓝图（N 是规划单元，不是重复次数——N 章要系统性推进主线/支线/伏笔/大纲，不能各自为政）。
若给定了【整书大纲】，每章 goal 必须沿所属卷目标/阶段目标推进，且不重复已写章节的开场/桥段；大纲与已写正文冲突时以已写正文为准。
输出严格 JSON：
{"chapters": [{"goal": "本章推进目标（哪条线/收哪些伏笔/新种钩子）", "outline_advance": "大纲推进段"}, ...]}  共 N 项"""


def _overlaps(item: dict, start: int, end: int) -> bool:
    try:
        lo, hi = int(item.get("chapter_start") or 0), int(item.get("chapter_end") or 0)
    except (TypeError, ValueError):
        return False
    return bool(lo and hi and lo <= end and hi >= start)


def _batch_plan_messages(batch: dict, outline: dict | None = None) -> list[dict]:
    """批次规划：只注入本批范围内的卷与阶段，不灌逐章细纲。"""
    user_parts = [f"起点章 {batch['start_chapter']}，N={batch['size']}。请输出 {batch['size']} 章推进蓝图（严格 JSON）。"]
    verdict = ((batch.get("current") or {}).get("audit_verdict") or {})
    if verdict.get("verdict") == "replan":
        user_parts.append("【上轮审核要求重规划的原因】" + json.dumps(verdict, ensure_ascii=False))
    if outline:
        objective = (outline.get("objective") or "").strip()
        volumes = outline.get("volumes") or []
        if not isinstance(volumes, list):
            volumes = []
        start, size = int(batch["start_chapter"]), int(batch["size"])
        end = start + size - 1
        if objective:
            user_parts.append(f"【全书 Objective（终局，沿此方向推进）】{objective}")
        rel_volumes, rel_stages = [], []
        for v in volumes:
            if not isinstance(v, dict):
                continue
            stages = [s for s in (v.get("stages") or [])
                      if isinstance(s, dict) and _overlaps(s, start, end)]
            if stages or _overlaps(v, start, end):
                rel_volumes.append(v)
                rel_stages.extend(stages)
        if rel_volumes:
            vol_lines = []
            for v in rel_volumes:
                krs = [str(k).strip() for k in (v.get("key_results") or []) if str(k).strip()]
                vline = (f"- 第 {v.get('volume_seq')} 卷《{v.get('title') or ''}》"
                         f"（第 {v.get('chapter_start')}–{v.get('chapter_end')} 章）：{v.get('goal') or ''}")
                if krs:
                    vline += "（KR：" + "；".join(krs) + "）"
                vol_lines.append(vline)
            user_parts.append("【涉及卷（本批次须贴卷目标/KR）】\n" + "\n".join(vol_lines))
        if rel_stages:
            user_parts.append("【涉及阶段（沿阶段目标推进，不要写成逐章细纲复述）】\n" + "\n".join(
                f"- {s.get('name')} 第 {s.get('chapter_start')}–{s.get('chapter_end')} 章：{s.get('goal') or ''}"
                for s in rel_stages))
    return [
        {"role": "system", "content": _BATCH_PLAN_PROMPT},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def node_batch_plan(state: BatchState) -> BatchState:
    pid = state["project_id"]
    with tenant_session(pid) as db:
        # 整书大纲注入（§11）：批次蓝图对齐卷目标/KR + 大纲位；无大纲回退原行为（各自为政会泛化）。
        outline = None
        row = repo.get_volume_outline(db, uuid.UUID(pid), 1)
        if row and isinstance(row.outline, dict):
            outline = normalize_outline(row.outline)
        messages = _batch_plan_messages(state, outline=outline)
        resp = make_chain("planner", db=db, project_id=pid).generate(messages, json_mode=True)
        nodes.record_run(db, project_id=pid, task_id=state.get("batch_task_id"),
                         node="batch_plan", role="Planner", resp=resp, error=resp.error, messages=messages)
        if resp.error:
            return {"batch_failed": True, "error": resp.error}
    try:
        data = nodes._parse_json(resp.content)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("batch_plan 顶层非 JSON 对象", resp.content, 0)
        chapters = data.get("chapters", [])
        # 合法 JSON 但形状不符（chapters 非数组）→ 显式失败，避免对 dict 迭代 TypeError
        if not isinstance(chapters, list):
            raise json.JSONDecodeError(
                f"chapters 字段非数组（实际 {type(chapters).__name__}）", resp.content, 0
            )
        # 少返回 → 显式失败（评审 A3）：position 按 0..size-1 推进，缺项会让批次
        # 以 failed 收尾却只写了部分章；多返回截断到 size（多余项不执行）
        if len(chapters) < state["size"]:
            raise json.JSONDecodeError(
                f"batch_plan 只返回 {len(chapters)} 项，需要 {state['size']} 项", resp.content, 0
            )
        chapters = chapters[: state["size"]]
    except (json.JSONDecodeError, KeyError) as exc:
        return {"batch_failed": True, "error": f"batch_plan 输出解析失败: {exc}"}
    for i, c in enumerate(chapters):
        c["seq"] = state["start_chapter"] + i
    return {"batch_plan": {"chapters": chapters}}


def make_chapter_runner(chapter_graph):
    """返回 chapter 节点函数：构造 ChapterState → invoke 单章子图 → 结果桥回批次（状态桥）。"""

    def node_run_chapter(state: BatchState) -> BatchState:
        plan = (state.get("batch_plan") or {}).get("chapters") or []
        position = state.get("position", 0)
        if position >= len(plan):
            return {"batch_failed": True, "error": "position 越界"}
        item = plan[position]
        chapter_seq = item["seq"]
        # 每章派生 thread，与 batch 主 thread 隔离（§6.12 续跑语义）
        thread_id = f"{state['batch_task_id']}:ch{chapter_seq}"

        # 手动暂停/取消守卫（§6.12 批次控制）：章节边界查 DB 任务状态——paused/cancelled
        # 中断批次。外部控制（POST .../pause|cancel）只改 DB 不产生 SSE 事件，图在此处
        # 感知并停下（尽力而为：本章跑完即停，非节点级中断）。抛 BatchHaltError 使
        # checkpoint 停在本章，resume 从本章续跑不重跑已完成章。
        with tenant_session(state["project_id"]) as db:
            from myink.models import Task
            # 批次推进到某章时先物化该章页面，随后该章的规划、写作、审核和费用
            # 才开始产生；刷新或切章时不会借用上一章的页面承载当前进度。
            repo.ensure_chapter_placeholder(
                db,
                project_id=uuid.UUID(state["project_id"]),
                chapter_seq=chapter_seq,
            )
            batch = db.get(Task, uuid.UUID(state["batch_task_id"]))
            if batch and batch.status in ("paused", "cancelled"):
                raise BatchHaltError(batch.status)

        # 确认流收尾（§6.11）：本章 awaiting_review（批次因 critical 暂停后人工确认候选、
        # resume 批次）→ 取本章 checkpoint 草稿直接落库正文，不重跑本章——LangGraph 1.2.9
        # resume 语义是整图从 START 重跑，确定性输入会再命中 critical → 永久暂停、正文
        # 永不落库（2026-08-17 用户实测：确认后点续跑变成重新生成）。
        with tenant_session(state["project_id"]) as db:
            ch = repo.get_chapter(db, uuid.UUID(state["project_id"]), chapter_seq)
        if ch is not None and ch.status == "awaiting_review":
            snap = chapter_graph.get_state({"configurable": {"thread_id": thread_id}})
            if snap.values:
                from myink.workflow.review import resolve_review
                result = resolve_review(chapter_graph, snap.values, task_id=thread_id)
                if result.get("error"):
                    raise BatchChapterError(result["error"])
                if result.get("needs_review"):
                    raise BatchReviewError(f"第 {chapter_seq} 章仍需人工评审")
                return {"current": result, "position": position,
                        "shared_context": state.get("shared_context")}

        chapter_input: ChapterState = {
            "project_id": state["project_id"],
            "chapter_seq": chapter_seq,
            "task_id": thread_id,
            "batch_goal": item.get("goal"),
            # 批次级共享上下文（§6.11 共享池）：稳定部分（hard_facts）传后续章复用
            "shared_context": state.get("shared_context"),
        }
        try:
            result = chapter_graph.invoke(
                chapter_input, config={"configurable": {"thread_id": thread_id}, "recursion_limit": 64}
            )
        except Exception as exc:  # 子图异常 → 抛中断，batch checkpoint 停在 chapter 节点可续跑
            logger.exception("单章 %s 失败", chapter_seq)
            raise BatchChapterError(f"单章 {chapter_seq} 失败: {exc}") from exc
        if result.get("error"):  # 子图优雅失败（error 返回非异常）→ 同样中断批次，不走到 batch_end
            logger.error("单章 %s 失败: %s", chapter_seq, result["error"])
            raise BatchChapterError(f"单章 {chapter_seq} 失败: {result['error']}")
        if result.get("replan_batch") and state.get("batch_replan_count", 0) >= settings.max_replans:
            result.update(nodes.node_persist({**result, "replan_batch": False, "needs_review": True}))
            result["replan_batch"] = False
        # critical 冲突（§6.11 确认分流）：暂停批次转人工。抛异常而非走 batch_paused——
        # batch_paused→batch_end→END 后续跑只能整批重跑；抛 BatchReviewError 使
        # checkpoint 停在本章（position 未推进），人工确认候选后 resume 从本章续跑。
        report = result.get("report") or {}
        if result.get("needs_review"):
            raise BatchReviewError(
                f"第 {chapter_seq} 章存在 critical 冲突，暂停批次转人工（§6.11）"
            )
        # 首章 recall 组装共享上下文 → 桥回批次，后续章复用（§6.11）
        shared = result.get("shared_context") or state.get("shared_context")
        return {"current": result, "position": position, "shared_context": shared}

    return node_run_chapter


def node_reset_replan_batch(state: BatchState) -> BatchState:
    """replan(batch) 回 batch_plan 前清信号：position 回退到本章（不推进），重规划剩余章。

    设计：audit 判 replan_target=batch → 批次蓝图走偏，整批剩余章重规划。重置
    replan_batch 信号 + 保留 position（batch_plan 重规划后本章重跑）；不重跑已完成章。
    """
    return {"replan_batch": False, "batch_replan_count": state.get("batch_replan_count", 0) + 1}


def route_after_chapter(state: BatchState) -> str:
    """批次路由（spec/state-flow.md §3，已确认策略）。

    critical 冲突已在 node_run_chapter 抛 BatchReviewError 转人工（§6.11），
    不会走到这里；此处只处理正常推进 / 单章失败 / replan / 批次完结。
    """
    current = state.get("current") or {}
    if state.get("batch_failed") or current.get("error"):
        return "batch_failed"
    if current.get("replan_batch"):  # 审核判 replan(batch) → 回 batch_plan 重规划剩余章（§6.11）
        return "replan_batch"
    position = state.get("position", 0)
    if position + 1 < state.get("size", 0):
        return "next_chapter"
    return "batch_done"


def node_reflexion(state: BatchState) -> BatchState:
    """批次收尾复盘（§8.9 reflexion）：读整批 audit findings → 复发率记账 → LLM 总结演化写作经验。

    增强项不阻塞批次：LLM 失败 / 解析失败只记 error，批次照常 batch_end（与 batch_plan 的
    batch_failed 不同——复盘是加分项，不是创作主线）。失败批不走到这里（batch_failed→batch_end）。
    """
    pid = state["project_id"]
    batch_task_id = state["batch_task_id"]
    start = state["start_chapter"]
    try:
        with tenant_session(pid) as db:
            if nodes._batch_already_reflexed(db, pid, batch_task_id):
                return {"reflexion": {"skipped": "already_reflexed"}}
            findings = nodes._collect_batch_audit_findings(db, batch_task_id)
            recurrences = nodes._update_recurrences(db, pid, findings, start)
            new_findings = nodes._covered_by_active(db, pid, findings, start)
            if not new_findings:
                nodes.record_plain(db, project_id=pid, task_id=batch_task_id, node="reflexion",
                                   detail={"findings": len(findings), "recurrences": recurrences,
                                           "lessons": 0, "reason": "no_new_findings"})
                return {"reflexion": {"findings": len(findings), "recurrences": recurrences,
                                      "lessons": 0, "reason": "no_new_findings"}}
            existing = repo.get_active_lessons(db, uuid.UUID(pid))
            messages = prompts.reflexion_messages(new_findings, [
                {"category": l.category, "content": l.content, "recurrence_count": l.recurrence_count}
                for l in existing
            ], start, state["size"])
            resp = make_chain("audit", db=db, project_id=pid).generate(messages, json_mode=True,
                                                max_tokens=nodes._MAX_TOKENS["reflexion"])
            nodes.record_run(db, project_id=pid, task_id=batch_task_id, node="reflexion",
                             role="Reflexion", resp=resp, error=resp.error, messages=messages,
                             detail={"findings": len(findings), "recurrences": recurrences})
            if resp.error:
                return {"reflexion": {"error": resp.error}}
            data = nodes._parse_json(resp.content)
            lessons = data.get("lessons", []) if isinstance(data, dict) else []
            inserted, skipped = nodes._persist_lessons(db, pid, batch_task_id, start, lessons, new_findings)
            return {"reflexion": {"findings": len(findings), "recurrences": recurrences,
                                  "lessons": inserted, "skipped_duplicates": skipped}}
    except Exception as exc:
        logger.warning("reflexion 复盘失败（不阻塞批次）: %s", exc)
        return {"reflexion": {"error": str(exc)}}


def node_global_audit(state: BatchState) -> BatchState:
    """批次收尾全局审计：窗口长度 >= K 时对照卷规划做推进审计。

    非阻塞（镜像 node_reflexion）：below_threshold 短路零成本、不落报告；LLM/解析
    失败仍写 status=failed 报告并推进 marker（不阻塞批次，防每批重审同一毒窗口）。
    增强项失败不 batch_failed——审计是加分项，不是创作主线。
    """
    pid = state["project_id"]
    try:
        with tenant_session(pid) as db:
            win = ga.window_for_batch(db, pid, K=settings.audit_interval)
            if win is None:
                return {"global_audit": {"triggered": False, "reason": "below_threshold"}}
            return {"global_audit": ga.run_global_audit(
                db, pid, win, source="batch", source_batch_task_id=state["batch_task_id"])}
    except Exception as exc:
        logger.warning("全局审计失败（不阻塞批次）: %s", exc)
        return {"global_audit": {"error": str(exc)}}


def node_batch_end(state: BatchState) -> BatchState:
    """批次收尾：汇总报告 + 更新任务状态（§6.8 批次成本展示）。"""
    pid = state["project_id"]
    status = "failed" if state.get("batch_failed") else "paused" if state.get("batch_paused") else "done"
    position = state.get("position", 0)
    summary = {
        "size": state.get("size"),
        "start_chapter": state.get("start_chapter"),
        "position": position,
        "completed": position + 1 if not state.get("batch_failed") else position,  # 已完成章数
        "status": status,
        "error": state.get("error"),
    }
    # reflexion 复盘指标（§8.9）：findings / recurrences / lessons，随批次汇总暴露
    if state.get("reflexion"):
        summary["reflexion"] = state["reflexion"]
    # 全局审计指标（§8.6）：triggered/窗口/findings/status，随批次汇总暴露
    if state.get("global_audit"):
        summary["global_audit"] = state["global_audit"]
    with tenant_session(pid) as db:
        from myink.models import AgentRun, Task
        # 每章 run 的 task_id = {batch_task_id}:ch{seq}，batch_plan 一次 run 用裸
        # batch_task_id——必须前缀匹配才不漏章成本（精确匹配只统计到 batch_plan）。
        runs = db.query(AgentRun).filter(AgentRun.task_id.like(f"{state['batch_task_id']}%")).all()
        summary["total_cost"] = round(sum(r.cost_est for r in runs), 6)
        summary["total_duration_ms"] = sum(r.duration_ms for r in runs)
        summary["degraded_runs"] = sum(1 for r in runs if r.degraded)
        task = db.get(Task, uuid.UUID(state["batch_task_id"]))
        if task:
            task.status = status
    logger.info("批次汇总: %s", summary)
    return {"batch_summary": summary}


def build_batch_graph(chapter_graph, checkpointer=None):
    node_run_chapter = make_chapter_runner(chapter_graph)

    g = StateGraph(BatchState)
    g.add_node("batch_plan", node_batch_plan)
    g.add_node("chapter", node_run_chapter)
    g.add_node("bridge", lambda s: {"position": (s.get("position") or 0) + 1})
    g.add_node("reset_replan_batch", node_reset_replan_batch)
    g.add_node("reflexion", node_reflexion)
    g.add_node("global_audit", node_global_audit)
    g.add_node("batch_end", node_batch_end)

    g.add_edge(START, "batch_plan")
    g.add_conditional_edges(
        "batch_plan",
        lambda s: "run" if not s.get("batch_failed") else "fail",
        {"run": "chapter", "fail": "batch_end"},
    )
    g.add_conditional_edges(
        "chapter", route_after_chapter,
        {"next_chapter": "bridge",
         "batch_failed": "batch_end", "batch_done": "reflexion",
         "replan_batch": "reset_replan_batch"},
    )
    g.add_edge("bridge", "chapter")
    # replan(batch) → 回 batch_plan 重规划剩余章（position 保留本章，不推进）
    g.add_edge("reset_replan_batch", "batch_plan")
    # 批次收尾复盘（§8.9）：正常收尾才提炼；失败/暂停批跳过（batch_failed 直连 batch_end）
    # 复盘后接全局审计（§8.6，每 K 章触发，below_threshold 短路零成本），再 batch_end
    g.add_edge("reflexion", "global_audit")
    g.add_edge("global_audit", "batch_end")
    g.add_edge("batch_end", END)
    return g.compile(checkpointer=checkpointer)
