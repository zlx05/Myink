"""Pydantic 契约模型 —— 严格映射 spec/schema.md 12 个 JSON Schema。

用途（plan.md §6.4）：extract / plan_chapter / validate 三方输出的唯一契约，
LangGraph 节点间传结构化对象；坏数据拒绝但不崩。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

# ---- 枚举（与 spec/schema.md / models.memory 一致）----
CharacterStateField = Literal["location", "injury", "realm", "power", "item", "knowledge", "goal", "identity", "alive"]
RelationType = Literal["hostile", "ally", "master_student", "located_in", "owns", "defeated_by", "knows", "promises", "happened_at"]
ForeshadowStatus = Literal["planted", "developing", "resolved", "dropped"]
PlotThreadKind = Literal["main", "side"]
PlotThreadStatus = Literal["active", "stalled", "closed"]
FactConfirmStatus = Literal["pending", "confirmed", "rejected", "expired"]
CandidateKind = Literal["event", "fact", "character_state", "relation_change", "foreshadow", "foreshadow_touch", "plotline", "chapter_summary", "memory_removal", "character_card", "new_entity"]
CandidateStatus = Literal["pending", "confirmed", "rejected"]
Severity = Literal["critical", "major", "minor", "hint"]
FindingSource = Literal["L1", "L2"]
ConflictType = Literal["faction", "power", "timeline", "location", "character", "character_state", "relation", "foreshadow", "item_rule", "plotline", "persona", "style"]


class CharacterState(BaseModel):
    """人物状态台账一行（追加式，§1）。"""

    project_id: uuid.UUID
    character_id: uuid.UUID
    chapter_seq: int = Field(ge=1)
    field: CharacterStateField
    old_value: str | None = None
    new_value: str | None = None
    source_chapter: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    valid_from: int = Field(default=1, ge=1)
    valid_to: int | None = Field(default=None, ge=1)


class Character(BaseModel):
    """人物静态基底（§2）。"""

    project_id: uuid.UUID
    name: str
    aliases: list[str] = Field(default_factory=list)
    race: str | None = None
    origin: str | None = None
    realm_cap: str = Field(description="境界上限（战力硬约束）")
    personality: str | None = Field(default=None, description="性格基调（漂移审计基线）")
    base_attrs: dict = Field(default_factory=dict)
    version: int = 1


class Scene(BaseModel):
    """场景（规划产物：LLM 只认识名字，不持库内 id；落库归一化在 extract 候选 §7.5）。"""

    location_id: str = Field(description="地点名（非 UUID，规划阶段无 id 语义）")
    participants: list[str] = Field(default_factory=list, description="人物名列表")
    goal: str
    time: str | None = None


class CharacterPresence(BaseModel):
    character_id: str = Field(description="人物名（非 UUID）")
    expected_state: dict = Field(default_factory=dict)


class ChapterTransition(BaseModel):
    """从原文章尾推导的接续方案；存入计划，供写作和修订共同遵守。"""

    mode: Literal["continue", "time_jump", "scene_cut", "opening"]
    anchor_quote: str = Field(description="上一章结尾逐字短引；首章/缺失正文填空")
    pending_action: str = Field(description="尚未完成的动作、问题或危险；没有则填空")
    opening_beat: str = Field(min_length=1, description="第一拍的新动作及其结果，不重演已完成动作")
    bridge: str = Field(description="转时空/视角的线索，以及悬念如何承接；直接接续可填空")


class ChapterPlan(BaseModel):
    """章节计划（规划 agent 输出，§3，大纲偏差比对输入）。"""

    project_id: uuid.UUID | None = None
    chapter_seq: int | None = Field(default=None, ge=1)
    goals: list[str] = Field(min_length=1)
    scenes: list[Scene] = Field(default_factory=list)
    characters: list[CharacterPresence] = Field(default_factory=list)
    hooks_to_plant: list[str] = Field(default_factory=list)
    hooks_to_resolve: list[str] = Field(default_factory=list, description="须命中池中开放伏笔")
    expected_events: list[str] = Field(min_length=1, description="预期事件")
    hard_constraints: list[str] = Field(default_factory=list)
    transition: ChapterTransition | None = None  # 兼容旧 checkpoint；新规划提示词要求输出


class ChapterCast(BaseModel):
    """本章出场人物与场景地点（规划第一拍，§3）。

    规划拆成有先后顺序的两拍，为的是解开「取人物状态需要出场人物、而出场人物本是规划
    产物」的循环：第一拍只定人名与地点，据此取台账（人物状态/设定实体/事件召回），
    第二拍才产出完整章节计划。
    """

    cast: list[str] = Field(min_length=1, description="本章出场人物名，须优先取自现有角色名单")
    locations: list[str] = Field(default_factory=list, description="本章场景地点名")


class Event(BaseModel):
    """剧情事件（中期记忆，§4）。"""

    project_id: uuid.UUID | None = None
    summary: str
    participants: list[uuid.UUID] = Field(default_factory=list, description="归一化 canonical id")
    location_id: uuid.UUID | None = None
    timeline: str | None = None
    related_threads: list[uuid.UUID] = Field(default_factory=list)
    source_chapter: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    promoted_to_fact: bool = False
    version: int = 1


class Fact(BaseModel):
    """长期事实（§5）。"""

    project_id: uuid.UUID | None = None
    content: str
    category: str | None = Field(default=None, description="世界观/身份/归属/关系/规则")
    is_hard: bool = False
    source_chapter: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    confirm_status: FactConfirmStatus = "pending"
    valid_from: int = Field(default=1, ge=1)
    valid_to: int | None = None
    version: int = 1


class Relation(BaseModel):
    """实体关系（带时间窗，§6）。"""

    project_id: uuid.UUID | None = None
    source_id: uuid.UUID
    relation_type: RelationType
    target_id: uuid.UUID
    properties: dict = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    source_chapter: int = Field(ge=1)
    version: int = 1
    valid_from: int = Field(default=1, ge=1)
    valid_to: int | None = None


class Foreshadow(BaseModel):
    """伏笔（状态机，§7）。"""

    project_id: uuid.UUID | None = None
    description: str
    status: ForeshadowStatus
    planted_chapter: int = Field(ge=1)
    resolved_chapter: int | None = Field(default=None, ge=1)
    trigger: dict = Field(default_factory=dict, description="回收条件：触发者+动作+对象")
    related_entities: list[uuid.UUID] = Field(default_factory=list)
    last_touched: int | None = Field(default=None, ge=1)


class PlotThread(BaseModel):
    """剧情线（§8）。"""

    project_id: uuid.UUID | None = None
    name: str
    kind: PlotThreadKind
    status: PlotThreadStatus
    priority: int = Field(default=1, ge=1)
    progress: str | None = None
    participants: list[uuid.UUID] = Field(default_factory=list)
    last_progress_chapter: int | None = Field(default=None, ge=1)
    open_duration: int | None = None


class EvidenceItem(BaseModel):
    chapter: int
    quote: str


class Finding(BaseModel):
    """校验发现（L1/L2 统一，§9，conflict_key 跨修订轮稳定）。"""

    finding_id: str | None = None
    conflict_key: str = Field(description="hash(类型+实体+位置)")
    conflict_type: ConflictType
    severity: Severity
    scope: Literal["local", "structural"] = "local"
    source: FindingSource = "L1"
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1, description="仅 L2 需要")
    suggestion: str | None = None


class MutationCandidate(BaseModel):
    """记忆候选（待确认池，§10）。"""

    kind: CandidateKind
    project_id: uuid.UUID | None = None
    source_chapter: int = Field(ge=1)
    payload: dict = Field(description="对应 kind 的对象体")
    confidence: float = Field(ge=0, le=1)
    status: CandidateStatus = "pending"


class ValidationReport(BaseModel):
    """校验报告（§11，L1 规则层产物，不被 LLM 绕过）。"""

    project_id: uuid.UUID
    chapter_seq: int = Field(ge=1)
    findings: list[Finding] = Field(default_factory=list)
    summary: dict = Field(default_factory=lambda: {"total": 0, "critical": 0, "resolved": 0})


class AuditVerdict(BaseModel):
    """审核中枢决策（§6.5，混合路由的 LLM 语义层产物）。

    verdict 做路由（pass / rewrite / replan）；但 L1 critical 与轮次预算由
    规则层强制（route_after_audit ①②），本模型只承载语义建议——决策可审计
    （reasons / confidence 落库）、可统计（路由分布）。
    """

    verdict: Literal["pass", "rewrite", "replan"]
    replan_target: Literal["chapter", "batch"] | None = Field(
        default=None, description="仅 verdict=replan 有效：重规划本章 or 剩余整批"
    )
    findings: list[Finding] = Field(default_factory=list, description="L2 语义校验（引用证据 + 置信度）")
    reasons: list[str] = Field(default_factory=list, description="路由决策理由（可审计）")
    confidence: float = Field(default=0.8, ge=0, le=1, description="决策置信度（低 → 规则兜底转人工）")


class ReflexionLesson(BaseModel):
    """复盘提炼单条写作经验（reflexion 节点 LLM 输出，node 里确定性分级路由）。

    conflict_type 一条经验对应一个冲突类型（跨类型拆多条）；content 为结合已有经验
    总结演化的跨章可复用表述（注入后续章节规划/写作 system 段）。
    """

    conflict_type: ConflictType
    lesson_type: Literal["planning", "writing", "both"] = "both"
    content: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)


class RetrievedContext(BaseModel):
    """召回上下文（recall 节点输出，§12）。"""

    long_term_facts: list[dict] = Field(default_factory=list, description="[{fact_id, source_chapter}]")
    mid_term_events: list[dict] = Field(default_factory=list, description="[{event_id, chapter, confidence}]")
    short_context: list[dict] = Field(default_factory=list, description="上一章摘要+候选事件+本章开头+最近场景")
    recent_openings: list[dict] = Field(default_factory=list, description="历史章开头 [{chapter, text}]，仅供差异化比较")
    entity_snapshots: list[dict] = Field(default_factory=list, description="人物/势力/地点当前状态快照")
    setting_snapshots: list[dict] = Field(default_factory=list, description="设定实体快照（物品/功法/地点，entities 表 §7.11 ④ 自动建档）[{entity_id, entity_type, name, description, first_seen_chapter}]")
    open_foreshadows: list[dict] = Field(default_factory=list, description="开放伏笔 [{description, trigger, planted_chapter, status}]（§7.9，plan_chapter 消费决定收/延/弃）")
    plot_threads: list[dict] = Field(default_factory=list, description="活跃剧情线 [{name, kind, status, progress}]（线程债务治理输入）")
    reflexions: list[dict] = Field(default_factory=list, description="本书写作经验（reflexion 注入，§8.9）[{content, lesson_type, category, source_chapter}]")
    token_usage: int = 0
    recall_stats: dict = Field(default_factory=dict,
                               description="{vector_hits, keyword_hits, fused_total, recall_tokens_est, context_tokens_est, share}（§16 召回占比，混合召回时填充）")
