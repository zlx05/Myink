"""LangGraph 状态定义（spec/state-flow.md 节点契约的代码形态）。

ChapterState / BatchState 都带 thread_id（= task_id），Checkpointer 按此断点续跑（§6.7）。
节点间传结构化对象（Pydantic model_dump），不传不断变长的自然语言 Prompt。
"""

from __future__ import annotations

from typing import TypedDict


class ChapterState(TypedDict, total=False):
    """单章子图状态。"""

    project_id: str
    chapter_seq: int
    task_id: str  # thread_id（单章任务 = task_id；批次内 = batch_task_id 派生）
    user_instruction: str | None
    rewrite: bool  # 显式重写已确认章（§7.3 失效重建触发，persist 先失效旧记忆再写新）
    review_revision: bool  # 人工否定设定后的修订，不重放旧稿的已确认候选
    writing_mode: str  # auto / manual；manual 在每次 plan_chapter 后等待用户确认

    # load_state 产物
    settings: dict
    characters: list[dict]
    chapter_plan_input: dict
    style_profile: dict | None  # 文风档案（§7.12，写章生成约束）
    genre_pack: dict | None  # 本书题材包快照（节奏/禁忌/机制）
    target_words: int | None  # 单章目标字数（§6.9，可配置）

    # recall 产物
    context: dict  # RetrievedContext.model_dump()

    # plan_cast 产物（规划第一拍：先定出场人物/场景地点，据此重取 context）
    cast: dict  # ChapterCast.model_dump()

    # plan_chapter 产物
    plan: dict  # ChapterPlan.model_dump()
    batch_goal: str | None  # 批次推进目标（§6.11）

    # write 产物
    draft: str

    # extract 产物
    candidates: list[dict]  # MutationCandidate.model_dump()

    # validate 产物（L1 规则层，不被 LLM 绕过）
    report: dict  # ValidationReport.model_dump()
    unresolved: list[dict]  # 未解决 finding（critical/major）

    # audit 产物（审核中枢，混合路由 LLM 语义层）
    audit_verdict: dict  # AuditVerdict.model_dump()
    replan_count: int  # replan 轮次（max_replans = 1）
    replan_batch: bool  # verdict=replan 且 replan_target=batch → 批次层回 batch_plan

    # 批次级共享上下文（§6.11 共享池：recall 稳定部分批次内一次组装、各章复用）
    shared_context: dict  # {"hard_facts": [...], "settings": {...}}

    # revise 产物
    revision_count: int
    revise_responses: list[dict]

    # 只读查证工具调用痕迹（§10：audit/write 持只读工具，仅记录不落库，纯 checkpoint）
    tool_trace: list[dict]  # [{tool, arguments, result[:200]}]

    # persist 产物 / 失败
    persisted: bool
    needs_review: bool  # 转人工（§6.11 确认分流）
    error: str | None


class BatchState(TypedDict, total=False):
    """批次主图状态（§6.11 自动写作批次）。"""

    project_id: str
    batch_task_id: str  # thread_id = batch_task_id，整批可续跑
    size: int  # N（规划单元，不是重复次数）
    position: int  # 当前章下标（0-based）
    start_chapter: int  # 起点章号

    # batch_plan 产物
    batch_plan: dict  # {chapters: [{seq, goal, outline_advance}]}

    # 当前章状态（状态桥：上章 persist 结果 → 下章 recall 输入）
    current: dict | None  # ChapterState 的镜像

    # 批次级共享上下文（§6.11 共享池）：recall 稳定部分（设定/长期事实/台账）批次内组装一次
    shared_context: dict

    # 批次结果
    completed: list[dict]
    batch_replan_count: int
    batch_failed: bool
    batch_paused: bool
    replan_batch: bool  # 本章 audit 判 replan(batch) → 回 batch_plan 重规划剩余章
    error: str | None
    reflexion: dict  # 复盘提炼指标（§8.9：findings/recurrences/lessons，随 batch_summary 暴露）
    global_audit: dict  # 全局审计指标（§8.6：triggered/窗口/findings/status，随 batch_summary 暴露）
    batch_summary: dict  # batch_end 汇总（状态/成本/耗时）
