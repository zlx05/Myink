"""HTTP 响应契约模型（response_model 单一事实源，阶段 5 契约测试形式化）。

字段从各路由手拼 dict 提取（main.py / routes_*.py），挂到路由装饰器 response_model=
后由 FastAPI 生成完整 OpenAPI schema；`aiink contract export` 导出到
spec/api-openapi.json。命名统一 `*Out` 后缀，避免与 `aiink.models` 的 ORM 重名。

自由形状字段（payload / style_profile / summary / evidence / findings / detail /
invalidation）用 `dict` / `list[dict]` 松类型——契约价值在字段名存在 + 形状稳定，
不做深层枚举（避免误伤现有响应）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuthResponse(BaseModel):
    token: str
    user_id: str
    expires_in: int


class ProjectOut(BaseModel):
    id: str
    title: str
    genre: str
    current_chapter: int
    target_words: int | None = None


class ChapterMetaOut(BaseModel):
    id: str
    chapter_seq: int
    title: str | None = None
    status: str
    word_count: int | None = None
    summary: str | None = None  # 章节摘要（§7 短期记忆，前端章节记忆区块展示）


class ChapterDetailOut(ChapterMetaOut):
    version: int
    content: str | None = None
    summary: str | None = None


class ChapterVersionOut(BaseModel):
    version: int
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    reason: str | None = None
    created_at: str | None = None


class ChapterVersionsOut(BaseModel):
    chapter_id: str
    chapter_seq: int
    current_version: int
    versions: list[ChapterVersionOut] = Field(default_factory=list)


class ContentUpdateOut(BaseModel):
    chapter_id: str
    chapter_seq: int
    status: str
    version: int


class CorrectMemoryOut(BaseModel):
    chapter_seq: int
    changeset: dict = Field(default_factory=dict)
    pool: dict = Field(default_factory=dict)
    no_op: bool


class DeletedChapterOut(BaseModel):
    chapter_seq: int
    title: str | None = None
    invalidation: dict = Field(default_factory=dict)


class DeleteChapterOut(BaseModel):
    deleted: list[DeletedChapterOut] = Field(default_factory=list)
    current_chapter: int


class DeleteProjectOut(BaseModel):
    """整本书删除结果（§阶段 6：硬删，FK 级联清业务表 + 显式清 checkpoint/Redis 残留）。"""

    project_id: str
    deleted: bool


class AgentRunOut(BaseModel):
    task_id: str | None = None  # 所属子线程：批次 = {batch_id}:ch{seq}，单章 = 裸任务 id（右栏按章过滤）
    node: str
    model_id: str | None = None
    input_tokens: int
    output_tokens: int
    cache_hit: bool
    duration_ms: int
    cost_est: float
    retry_count: int
    degraded: bool
    error: str | None = None
    detail: dict | None = None


class TaskProgressOut(BaseModel):
    current: int
    total: int


class TaskDetailOut(BaseModel):
    task_id: str
    task_type: str
    status: str
    payload: dict = Field(default_factory=dict)
    error: str | None = None
    retry_count: int
    trace_id: str | None = None
    chapter_seq: int | None = None
    batch_task_id: str | None = None
    created_at: str | None = None
    progress: TaskProgressOut | None = None
    cost_total: float = 0.0
    runs: list[AgentRunOut] = Field(default_factory=list)


class TaskControlOut(BaseModel):
    task_id: str
    status: str
    message: str | None = None


class TaskSummaryOut(BaseModel):
    """任务历史列表项（§阶段 4 任务视图）：轻量摘要不含 runs，点开再拉详情。

    批次进度为派生量（同 TaskDetailOut.progress 口径）：current = 该批 persist 节点
    去重章数，total = payload.size。观测表（tasks 无 RLS），new_session 普通连接可查。
    """

    task_id: str
    task_type: str
    status: str
    chapter_seq: int | None = None
    batch_size: int | None = None
    batch_current: int | None = None
    cost_total: float = 0.0
    error: str | None = None
    created_at: str | None = None


class MemoryCandidateOut(BaseModel):
    candidate_id: str
    kind: str
    source_chapter: int
    payload: dict = Field(default_factory=dict)
    confidence: float
    status: str
    created_at: str | None = None
    review: dict | None = None


class CandidateActionOut(BaseModel):
    candidate_id: str
    status: str


class WritingLessonOut(BaseModel):
    lesson_id: str
    category: str
    lesson_type: str
    content: str
    evidence: list[dict] = Field(default_factory=list)
    confidence: float
    source_chapter: int
    status: str
    recurrence_count: int
    last_recurrence_at: int | None = None
    created_at: str | None = None


class LessonActionOut(BaseModel):
    lesson_id: str
    status: str


class GenrePackOut(BaseModel):
    source_id: str | None = None
    source_name: str = ""
    secondary_id: str | None = None
    secondary_name: str | None = None
    selling_point: str = ""
    subgenres: list[dict] = Field(default_factory=list)
    taboos: list[str] = Field(default_factory=list)
    pacing: str = ""
    satisfaction: list[str] = Field(default_factory=list)
    mechanics: list[str] = Field(default_factory=list)
    world_hints: list[str] = Field(default_factory=list)


class GenreCatalogItemOut(GenrePackOut):
    id: str
    name: str
    group: str


class ProjectSettingsOut(BaseModel):
    style_profile: dict = Field(default_factory=dict)
    skill_pack: str | None = None
    genre_pack: dict = Field(default_factory=dict)
    model_routes: dict = Field(default_factory=dict)
    model_connections: list[dict] = Field(default_factory=list)
    version: int


class ModelListOut(BaseModel):
    """模型连接探针：拉取可用模型 id（设置页「获取模型列表」；中转不支持 /models 时 ok=False）。"""

    ok: bool
    models: list[str] = Field(default_factory=list)
    error: str | None = None


class ConnectionTestOut(BaseModel):
    """模型连接探针：ping 联通结果（ok + 耗时；reply 为截断回显，可能为空）。"""

    ok: bool
    latency_ms: int = 0
    reply: str | None = None
    error: str | None = None


class RankingsConfigOut(BaseModel):
    enabled: bool
    mcp_url: str
    timeout: int
    limit: int
    source: str
    tool: str


class EnvironmentOut(BaseModel):
    """账号级环境配置（模型连接/路由 + 思考模式 + MCP 扫榜）。"""

    model_routes: dict = Field(default_factory=dict)
    model_connections: list[dict] = Field(default_factory=list)
    rankings: RankingsConfigOut
    thinking_enabled: bool = False


class RankingsProbeOut(BaseModel):
    """MCP 扫榜探针：list_tools 联通结果。"""

    ok: bool
    tools: list[str] = Field(default_factory=list)
    error: str | None = None


class SkillPresetOut(BaseModel):
    id: str
    name: str
    genre: str
    style_profile: dict = Field(default_factory=dict)


class StyleDraftOut(BaseModel):
    draft: dict = Field(default_factory=dict)


class StyleProfileOut(BaseModel):
    style_profile: dict = Field(default_factory=dict)
    skill_pack: str | None = None
    version: int


class AuditRunOut(BaseModel):
    window_start: int
    window_end: int
    audited_up_to_chapter: int
    status: str
    sampled_characters: list[dict] = Field(default_factory=list)
    findings: list[dict] = Field(default_factory=list)
    error: str | None = None
    summary: dict = Field(default_factory=dict)


class GlobalAuditSummaryOut(BaseModel):
    report_id: str
    window_start: int
    window_end: int
    audited_up_to_chapter: int
    trigger: str
    status: str
    sampled: int
    findings: int
    chapters: int
    bridge: dict | None = None
    style: dict | None = None
    volume: dict | None = None
    error: str | None = None
    created_at: str | None = None


class GlobalAuditDetailOut(GlobalAuditSummaryOut):
    """详情：列表项 + 抽样角色 + findings 明细（覆盖 Summary 的 int 计数为 list，同前端 Omit 模式）。"""

    sampled_characters: list[dict] = Field(default_factory=list)
    findings: list[dict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class SetupDraftOut(BaseModel):
    """建书设定骨架草稿（§7.11 ②）：Planner 提案，可编辑、不落库；LLM 失败 → {} + error。"""

    draft: dict = Field(default_factory=dict)
    error: str | None = None


class SetupConfirmOut(BaseModel):
    ok: bool


class OutlineDraftOut(BaseModel):
    """整书大纲草稿（§11 建书 ③）：Planner 提案（Objective + 卷 + 逐章目标），可编辑、不落库；
    LLM 失败 → {} + error（§6.12 降级）。"""

    outline: dict = Field(default_factory=dict)
    error: str | None = None


class BookOutlineOut(BaseModel):
    """整书大纲读取/落库结果（§11：无大纲 → outline null，不 500）。"""

    outline: dict | None = None


class WorldViewOut(BaseModel):
    """世界观浏览（§7.11 设定是活数据）：settings 的 world_rules/hard_constraints + 势力/地点。"""

    world_rules: dict = Field(default_factory=dict)
    hard_constraints: list[str] = Field(default_factory=list)
    factions: list[dict] = Field(default_factory=list)
    locations: list[dict] = Field(default_factory=list)


class CharacterCardOut(BaseModel):
    """人物卡片（静态基底 + 当前状态台账，§7.7）：state 按当前章物化 {field: new_value}。"""

    id: str
    name: str
    race: str | None = None
    origin: str | None = None
    realm_cap: str
    personality: str | None = None
    base_attrs: dict = Field(default_factory=dict)
    state: dict = Field(default_factory=dict)


class EntityCardOut(BaseModel):
    """设定实体卡片（§7.11 ④ 自动建档：武器/功法/技能/地点低风险自动登记）。"""

    id: str
    entity_type: str
    name: str
    description: str | None = None
    first_seen_chapter: int | None = None


class GraphNodeOut(BaseModel):
    """世界拓扑节点（§9 图谱：4 类分组全量，建书设定即入图含孤立项）。"""

    id: str
    name: str
    type: str  # character / faction / location / entity
    realm_cap: str | None = None
    stance: str | None = None
    entity_type: str | None = None
    parent_id: str | None = None


class GraphEdgeOut(BaseModel):
    """世界拓扑边（§9 图谱：人物关系活跃/失效 + 地点层级）。"""

    source_id: str
    target_id: str
    edge_type: str  # 人物关系类型（hostile/ally/…）或 hierarchy（地点层级）
    confidence: float | None = None
    expired: bool = False  # valid_to 非空 → 已失效（前端虚线）
    source_chapter: int | None = None


class WorldGraphOut(BaseModel):
    """世界拓扑全量（§9 图谱前端：ECharts 力导向分组渲染）。"""

    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)


class ForeshadowOut(BaseModel):
    """伏笔池台账（§7.9 伏笔状态机：planted/developing/resolved/dropped）。"""

    id: str
    description: str
    status: str
    planted_chapter: int
    resolved_chapter: int | None = None
    trigger: dict = Field(default_factory=dict)


class StoryEventOut(BaseModel):
    """事件台账单条（§7.4 中期记忆全量：抽取节点落库后此前只写不读）。"""

    id: str
    summary: str
    # participants 落库是 canonical 人物 UUID 字符串（nodes.py §7.5），读出前翻成人名——
    # 裸 uuid 对前端不可读；已删角色丢弃。
    participants: list = Field(default_factory=list)
    source_chapter: int
    confidence: float


class CharacterStateChangeOut(BaseModel):
    """人物状态台账变更单条（§7.7 追加式：old_value → new_value @ 第 N 章）。

    直接透出 character_states 原始追加行——含 valid_to 非空的已失效行，
    get_character_state 会把它们过滤并折叠掉。
    """

    field: str
    old_value: str | None = None
    new_value: str | None = None
    chapter_seq: int
    source_chapter: int
    confidence: float
    valid_from: int | None = None
    valid_to: int | None = None


class RankingItemOut(BaseModel):
    """扫榜单条（§10）：外部榜单已 sanitize（allowlist 字段，只作灵感参考）。"""

    rank: int
    title: str
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    hot: str | None = None


class RankingsOut(BaseModel):
    """扫榜响应（§10）：source=remote（实时榜单）/ sample（降级样例）；error 为降级原因。"""

    source: str
    tool: str = ""
    fetched_at: str | None = None
    error: str | None = None
    items: list[RankingItemOut] = Field(default_factory=list)
