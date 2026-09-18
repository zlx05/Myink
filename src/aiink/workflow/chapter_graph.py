"""单章子图（spec/state-flow.md §1 内层循环）。

load_state → recall → plan_cast → plan_chapter → write → extract → validate → audit
  → route_after_audit（混合路由，§6.11 已确认：规则层优先，LLM 兜语义）：
     ① L1 critical / L2 major（规则层）→ rev < 预算 revise / 预算用尽 persist(needs_review)
     ② 预算用尽 → persist(needs_review)
     ③ 采纳 AuditVerdict：pass → persist / rewrite → revise / replan → 回 plan_cast
     或 replan_batch → 结束单章子图，批次层读 replan_batch 信号回 batch_plan

规划拆成两拍（§3 先后顺序）：plan_cast 先定本章出场人物与场景地点，据此重取召回上下文
（人物状态/设定实体/事件术语都依赖它），plan_chapter 才产出完整计划。replan 回 plan_cast
而非 plan_chapter——要重来的是「谁出场」，不只是「怎么写」。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from aiink.config import settings
from aiink.workflow import nodes
from aiink.workflow.state import ChapterState


def node_reset_replan(state: ChapterState) -> ChapterState:
    """replan 循环入口：清瞬态（旧 draft/candidates/report/verdict），计 replan_count。

    设计：audit 判 replan → 重新规划本章（plan_chapter）→ 重写 → 重新 extract/validate/audit。
    不清瞬态会让旧草稿/旧 finding 污染新一轮（§6.12 同类问题：残留状态短路）。
    """
    context = dict(state.get("context") or {})
    verdict = state.get("audit_verdict") or {}
    feedback = "重新规划本章，解决上一版计划的问题：" + "；".join(verdict.get("reasons") or [])
    feedback += "；".join(f.get("suggestion") or "" for f in verdict.get("findings") or [])
    context["short_context"] = [*(context.get("short_context") or []),
                                {"kind": "replan_feedback", "text": feedback}]
    return {
        "context": context,
        "draft": None, "candidates": [], "report": None, "unresolved": [],
        "audit_verdict": None, "replan_batch": False,
        "revision_count": 0, "revise_responses": [],
        "replan_count": state.get("replan_count", 0) + 1,
    }


def route_after_audit(state: ChapterState) -> str:
    """混合路由纯函数（spec/state-flow.md §3，2026-08-07 确认）。

    规则层（①L1 critical ②L2 major ③预算）优先——LLM 不能绕过硬约束、不能无限重写；
    LLM 语义层（④采纳 AuditVerdict）只在规则放行时生效。
    """
    if state.get("error"):
        return "fail"
    report = state.get("report") or {}
    l1_critical = (report.get("summary") or {}).get("critical", 0) or 0
    l2_major = (report.get("summary") or {}).get("l2_major", 0) or 0
    revision_exhausted = state.get("revision_count", 0) >= settings.max_revisions
    replan_exhausted = state.get("replan_count", 0) >= settings.max_replans
    if l1_critical or l2_major:
        # 规则层：L1 critical / L2 major（正文-台账语义矛盾）或预算用尽 → 还能修则修，
        # 否则转人工（persist 按 critical/l2_major 分流进待确认池）
        if not revision_exhausted:
            return "revise"
        return "needs_review"
    verdict = (state.get("audit_verdict") or {}).get("verdict")
    semantic_block = any(f.get("severity") in ("critical", "major")
                         for f in (state.get("audit_verdict") or {}).get("findings", []))
    if verdict == "rewrite" or (verdict == "pass" and semantic_block):
        return "needs_review" if revision_exhausted else "revise"
    if verdict == "replan":
        if replan_exhausted:
            return "needs_review"
        return "replan_chapter" if ((state.get("audit_verdict") or {}).get("replan_target") == "chapter" or ":ch" not in (state.get("task_id") or "")) \
            else "replan_batch"
    return "persist" if verdict == "pass" else "needs_review"


def node_route(state: ChapterState) -> ChapterState:
    route = route_after_audit(state)
    with nodes.tenant_session(state["project_id"]) as db:
        nodes.record_plain(db, project_id=state["project_id"], task_id=state.get("task_id"),
                           node="route", detail={"route": route,
                           "audit_verdict": state.get("audit_verdict"),
                           "revision_count": state.get("revision_count", 0),
                           "replan_count": state.get("replan_count", 0),
                           "rule_summary": (state.get("report") or {}).get("summary", {})})
    return {"needs_review": route == "needs_review",
            "replan_batch": route == "replan_batch"}


def build_chapter_graph(checkpointer=None, *, entry: str = "load_state"):
    g = StateGraph(ChapterState)
    g.add_node("load_state", nodes.node_load_state)
    g.add_node("recall", nodes.node_recall)
    g.add_node("plan_cast", nodes.node_plan_cast)
    g.add_node("plan_chapter", nodes.node_plan_chapter)
    g.add_node("plan_gate", nodes.node_plan_gate)
    g.add_node("write", nodes.node_write)
    g.add_node("extract", nodes.node_extract)
    g.add_node("validate", nodes.node_validate)
    g.add_node("audit", nodes.node_audit)
    g.add_node("revise", nodes.node_revise)
    g.add_node("persist", nodes.node_persist)
    g.add_node("summarize", nodes.node_summarize)
    g.add_node("reset_replan", node_reset_replan)
    g.add_node("route", node_route)

    g.add_edge(START, entry)
    g.add_edge("load_state", "recall")
    g.add_edge("recall", "plan_cast")
    g.add_edge("plan_cast", "plan_chapter")
    g.add_edge("plan_chapter", "plan_gate")
    g.add_edge("plan_gate", "write")
    g.add_edge("write", "extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", "audit")
    g.add_edge("audit", "route")
    g.add_conditional_edges(
        "route", route_after_audit,
        {
            "persist": "persist",
            "revise": "revise",
            "replan_chapter": "reset_replan",
            "replan_batch": END,   # 单章子图结束，批次层读 replan_batch 信号
            "needs_review": "persist",  # persist 按 critical 决定 awaiting_review/落库（spec §3）
            "fail": END,
        },
    )
    g.add_edge("reset_replan", "plan_cast")
    # 修订改变了正文：重新抽取记忆并校验，不能持旧候选/旧报告审核新稿。
    g.add_edge("revise", "extract")
    g.add_edge("persist", "summarize")
    g.add_edge("summarize", END)
    return g.compile(checkpointer=checkpointer)
