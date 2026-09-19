// API 类型（字段对齐 Python 侧已核实响应形状，见 plan.md 阶段 4 首切计划 §7）

export interface AuthResponse {
  token: string
  user_id: string
  username: string
  tier: string
  role: 'user' | 'admin'
  expires_in: number
}

export interface AuthSessionResponse {
  user_id: string
  username: string
  tier: string
  role: 'user' | 'admin'
}

export interface OkResponse {
  ok: true
}

export interface Project {
  id: string
  title: string
  genre: string
  current_chapter: number
  /** 每章目标字数（§6.9 三层字数控制：max_tokens 换算 + L1 长度门禁 + prompt 注入） */
  target_words: number | null
  creation_status?: 'draft' | 'setup_confirmed' | 'ready' | 'legacy_ready'
}

export interface ProjectCreation {
  project: Project
  context: {
    premise?: string
    chapter_count?: number
    storyline?: string
    setup_draft?: Record<string, unknown>
    outline_draft?: Partial<BookOutline>
    setup_error?: string | null
    outline_error?: string | null
  }
}

export type ChapterStatus =
  | 'planning'
  | 'writing'
  | 'awaiting_review'
  | 'confirmed'
  | 'failed'
  | 'cancelled'

export interface ChapterMeta {
  id: string
  chapter_seq: number
  title: string | null
  status: ChapterStatus
  /** 正文字符数（len 口径，与 L1 字数门禁同源 §6.9；章节列表/正文侧展示） */
  word_count: number | null
  /** 章节摘要（§7 短期记忆：该章发生了什么，前端章节记忆区块展示） */
  summary: string | null
}

/** 单章详情（get_chapter，main.py:117；无 version——version 由保存响应返回） */
export interface ChapterDetail extends ChapterMeta {
  version: number
  content: string | null
  summary: string | null
}

export interface ContentUpdateResponse {
  chapter_id: string
  chapter_seq: number
  status: string
  version: number
}

/** 章节历史版本（阶段 4 版本表：覆盖写前快照；reason = manual/batch/revise/revert） */
export interface ChapterVersion {
  version: number
  title: string | null
  content: string | null
  summary: string | null
  reason: string
  created_at: string | null
}

/** 版本列表响应（versions 降序；当前实时版本 = current_version，不在列表中） */
export interface ChapterVersionsResponse {
  chapter_id: string
  chapter_seq: number
  current_version: number
  versions: ChapterVersion[]
}

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'paused'
  | 'awaiting_plan'
  | 'awaiting_review'
  | 'failed'
  | 'cancelled'
  | 'done'

export interface GenerateResponse {
  task_id: string
  trace_id: string
  status: string
}

/** 批次控制响应（POST /batches/:id/:action → Python tasks/{id}/{action}） */
export interface TaskControlResponse {
  task_id: string
  status: TaskStatus
  message?: string
}

export type RunNode =
  | 'load_state'
  | 'recall'
  | 'plan_cast'
  | 'plan_chapter'
  | 'plan_review'
  | 'write'
  | 'extract'
  | 'validate'
  | 'revise'
  | 'audit'
  | 'persist'
  | 'batch_plan'
  | 'batch_end'
  | 'global_audit'
  | 'style_extract'
  | (string & {})

export interface AgentRun {
  /** 所属子线程：批次 = {batch_id}:ch{seq}，单章 = 裸任务 id（右栏按选中章过滤流转） */
  task_id?: string | null
  node: RunNode
  model_id: string | null
  input_tokens: number
  output_tokens: number
  cache_hit: boolean
  duration_ms: number
  cost_est: number
  retry_count: number
  degraded: boolean
  error: string | null
  /** 确定性节点记执行统计；audit 行带 audit_verdict */
  detail: {
    audit_verdict?: AuditVerdict
    route?: string
    revision_count?: number
    replan_count?: number
    rule_summary?: Record<string, number>
    plan?: ChapterPlan
    plan_attempt?: number
    writing_mode?: WritingMode
    original_plan?: ChapterPlan
    approved_plan?: ChapterPlan
    changed?: boolean
    status?: string
  } | null
}

export type WritingMode = 'auto' | 'manual'

export interface ChapterPlanScene {
  location_id: string
  participants: string[]
  goal: string
  time?: string | null
}

export interface ChapterPlanCharacter {
  character_id: string
  expected_state: Record<string, unknown>
}

export interface ChapterPlanTransition {
  mode: 'continue' | 'time_jump' | 'scene_cut' | 'opening'
  anchor_quote: string
  pending_action: string
  opening_beat: string
  bridge: string
}

export interface ChapterPlan {
  project_id?: string | null
  chapter_seq?: number | null
  goals: string[]
  scenes: ChapterPlanScene[]
  characters: ChapterPlanCharacter[]
  hooks_to_plant: string[]
  hooks_to_resolve: string[]
  expected_events: string[]
  hard_constraints: string[]
  transition?: ChapterPlanTransition | null
}

export type Verdict = 'pass' | 'rewrite' | 'replan'

export interface AuditVerdict {
  verdict: Verdict
  replan_target?: 'chapter' | 'batch' | null
  findings: Finding[]
  reasons: string[]
  confidence: number
}

export type FindingSeverity = 'critical' | 'major' | 'minor' | 'hint'

export interface Finding {
  conflict_key: string
  conflict_type: string
  severity: FindingSeverity
  scope: 'local' | 'structural'
  source: string
  evidence: Array<{ chapter: number; quote: string }>
  suggestion: string | null
}

export interface TaskDetail {
  task_id: string
  task_type: 'chapter_generate' | 'batch_generate' | string
  status: TaskStatus
  payload: Record<string, unknown>
  error: string | null
  retry_count: number
  trace_id: string | null
  chapter_seq: number | null
  batch_task_id: string | null
  created_at: string | null
  /** 仅批次任务 */
  progress?: { current: number; total: number }
  /** 任务总花费（agent_runs.cost_est 求和，§6.8 成本透明）：单章=该章，批次=全批 */
  cost_total: number
  runs: AgentRun[]
}

/** 项目任务历史列表项（GET /projects/:pid/tasks → Python TaskSummaryOut，阶段 4 任务视图）。
 * 轻量摘要不含 runs，点开任一条再走 GET /tasks/:id 拉节点流转记录。 */
export interface TaskSummary {
  task_id: string
  task_type: 'chapter_generate' | 'batch_generate' | string
  status: TaskStatus
  chapter_seq: number | null
  /** 批次任务：目标章数（payload.size）；单章任务为 null */
  batch_size: number | null
  /** 批次任务：已完成章数（该批 persist 节点去重派生）；单章任务为 null */
  batch_current: number | null
  /** 任务总花费（§6.8 成本透明；批次=全批聚合） */
  cost_total: number
  error: string | null
  created_at: string | null
}

/** 记忆候选 kind（memory_candidates 表 CHECK 枚举，§7.3 事实生命周期） */
export type CandidateKind =
  | 'event'
  | 'fact'
  | 'character_state'
  | 'relation_change'
  | 'foreshadow'
  | 'foreshadow_touch'
  | 'chapter_summary'
  | 'memory_removal'
  | 'character_card'
  | 'new_entity'
  | (string & {})

/** 待确认候选（list_candidates，routes_candidates.py；payload 为自由 dict） */
export interface MemoryCandidate {
  candidate_id: string
  kind: CandidateKind
  source_chapter: number
  payload: Record<string, unknown>
  confidence: number
  status: 'pending' | 'confirmed' | 'rejected'
  review?: { mode?: "revise" | "memory_only"; reason?: string; applied?: boolean; superseded?: boolean } | null
  created_at: string | null
}

export interface CandidateActionResponse {
  candidate_id: string
  status: 'confirmed' | 'rejected'
}

/** 校正记忆响应（POST chapters/:cid/correct-memory → Python CorrectMemoryOut，§7.3） */
export interface CorrectMemoryResponse {
  chapter_seq: number
  /** 该章编辑后重新抽取 → 与已落库记忆 diff 出的变更集（进待确认池） */
  changeset: Record<string, unknown>
  pool: Record<string, unknown>
  /** 无差异 → true（前端提示「无变更」，不打扰） */
  no_op: boolean
}

/** 级联删章明细（DELETE chapters/:cid → Python DeleteChapterOut：删该章及其后全部章节） */
export interface DeletedChapter {
  chapter_seq: number
  title: string | null
  invalidation: Record<string, unknown>
}

export interface DeleteChapterResponse {
  deleted: DeletedChapter[]
  /** 删完后的当前最大章序 */
  current_chapter: number
}

/** 整本书删除结果（DELETE /projects/:pid → Python DeleteProjectOut，阶段 6 硬删） */
export interface DeleteProjectResponse {
  project_id: string
  deleted: boolean
}

/** 写作经验（GET lessons → Python WritingLessonOut，§8.9 reflexion：批次复盘高危经验） */
export interface WritingLesson {
  lesson_id: string
  category: string
  lesson_type: string
  content: string
  evidence: Array<Record<string, unknown>>
  confidence: number
  source_chapter: number
  /** proposed 待确认 → confirm/reject → active/rejected */
  status: 'proposed' | 'active' | 'rejected' | (string & {})
  recurrence_count: number
  last_recurrence_at: number | null
  created_at: string | null
}

export interface LessonActionResponse {
  lesson_id: string
  status: 'active' | 'rejected' | (string & {})
}

/** 文风档案（§7.12 StyleProfile：键值透传，validate_profile 顶层 None 丢弃） */
export type StyleProfile = Record<string, unknown>

export type ModelProtocol = 'openai' | 'anthropic'

export interface ModelConnection {
  id: string
  name: string
  protocol: ModelProtocol
  base_url: string
  model: string
  input_price?: number | null
  output_price?: number | null
  has_api_key: boolean
}

export interface ModelConnectionInput extends Omit<ModelConnection, 'has_api_key'> {
  api_key?: string
}

/** 创作设置（GET/PUT settings；文风在书内，题材包是另一层） */
export interface ProjectSettings {
  style_profile: StyleProfile
  skill_pack: string | null
  genre_pack: import('./lib/genrePacks').BookGenrePack | Record<string, never>
  model_routes: Record<string, string>
  model_connections: ModelConnection[]
  version: number
}

/** MCP 扫榜覆盖（账号级环境配置） */
export interface RankingsConfig {
  enabled: boolean
  mcp_url: string
  timeout: number
  limit: number
  source: string
  tool: string
}

export interface RankingsConfigInput {
  enabled?: boolean
  mcp_url?: string
  timeout?: number
  limit?: number
  source?: string
  tool?: string
}

/** 账号级环境配置（GET/PUT /environment） */
export interface EnvironmentSettings {
  model_routes: Record<string, string>
  model_connections: ModelConnection[]
  rankings: RankingsConfig
  thinking_enabled: boolean
}

export interface RankingsProbeRequest {
  mcp_url: string
  timeout?: number
}

export interface RankingsProbeResult {
  ok: boolean
  tools: string[]
  error: string | null
}

/** 模型连接探针请求（未保存的新连接传明文 api_key；已保存连接可传 connection_id 复用密钥） */
export interface ModelProbeRequest {
  protocol: ModelProtocol
  base_url: string
  model?: string
  api_key?: string
  connection_id?: string
}

/** 拉取可用模型列表（settings/models；中转不支持 /models 时 ok=false + error） */
export interface ModelListResult {
  ok: boolean
  models: string[]
  error: string | null
}

/** 联通测试结果（settings/test-connection；reply 为截断回显，可能为空） */
export interface ConnectionTestResult {
  ok: boolean
  latency_ms: number
  reply: string | null
  error: string | null
}

/** 题材 Skill 预设（skill-presets，routes_style.py；id 即 skill_pack marker） */
export interface SkillPreset {
  id: string
  name: string
  genre: string
  style_profile: StyleProfile
}

/** 文风样本提取响应（style-samples：统计层 + LLM 提炼草稿；extract_error 为 LLM 降级提示） */
export interface StyleDraft {
  draft: StyleProfile & { extract_error?: string }
}

/** 文风档案确认落库响应（style-profile：确认 + 可选 skill_pack 原子写） */
export interface StyleProfileResponse {
  style_profile: StyleProfile
  skill_pack: string | null
  version: number
}

/** 全局审计报告（GET global-audit 列表项，routes_global_audit.py；findings 明细在详情） */
export interface GlobalAuditReportSummary {
  report_id: string
  window_start: number
  window_end: number
  audited_up_to_chapter: number
  trigger: 'batch' | 'manual' | (string & {})
  status: string
  sampled: number
  findings: number
  chapters: number
  bridge?: { pairs: number; findings: number } | null
  style?: { sampled: number; findings: number } | null
  volume?: { volumes: number; stages: number } | null
  error: string | null
  created_at: string | null
}

/** 全局审计报告详情（GET global-audit/:id = 列表项 + 抽样角色 + findings 明细） */
export interface GlobalAuditReportDetail extends Omit<GlobalAuditReportSummary, 'findings'> {
  findings: Finding[]
  sampled_characters: Array<{ character_id: string; name: string }>
  summary: Record<string, unknown>
}

/** 手动触发全局审计响应（POST global-audit：run_global_audit 报告 dict，无 report_id/trigger） */
export interface AuditRunResponse {
  window_start: number
  window_end: number
  audited_up_to_chapter: number
  status: string
  sampled_characters: Array<{ character_id: string; name: string }>
  findings: Finding[]
  error: string | null
  summary: Record<string, unknown>
}

/** 建书（§7.11 建书向导）：POST /projects 创建 Project + 空 ProjectSettings（不调 LLM） */
export interface CreateProjectBody {
  request_id?: string
  premise?: string
  chapter_count?: number
  storyline?: string
  title: string
  genre?: string
  /** 传入（含 null）即走题材包建书，显示名锁定为包名 */
  primary_id?: string | null
  secondary_id?: string | null
  genre_fields?: import('./lib/genrePacks').GenreFields
  /** 每章目标字数（可选；500–20000，默认 3000） */
  target_words?: number
}

/** 作品信息更新（§6.9 每章目标字数可配）：PUT /projects/:pid，未传字段不改；显式 null 置空 */
export interface UpdateProjectBody {
  title?: string
  genre?: string
  target_words?: number | null
}

/** 设定骨架草稿请求（§7.11 ②：一句话梗概 → Planner 提案） */
export interface SetupDraftBody {
  premise: string
}

/** 设定骨架草稿响应（可编辑不落库；LLM 失败 → draft:{} + error 降级） */
export interface SetupDraft {
  draft: Record<string, unknown>
  error: string | null
}

/** 设定确认落库（§7.11 ③ append-only：world_rules/hard_constraints 整体替换、角色/势力/地点按 name 建） */
export interface SetupBody {
  world_rules: Record<string, unknown>
  hard_constraints: string[]
  characters: Array<Record<string, unknown>>
  forces: Array<Record<string, unknown>>
  locations: Array<{ name: string }>
}

export interface SetupConfirmResponse {
  ok: boolean
}

/** 整书大纲阶段（约 30 章一段） */
export interface OutlineStage {
  stage_seq?: number
  name: string
  chapter_start?: number
  chapter_end?: number
  goal: string
  beats?: string[]
}

/** 整书大纲卷（题材决定卷跨度；卷下是阶段，不是逐章） */
export interface OutlineVolume {
  volume_seq?: number
  title: string
  theme?: string
  goal: string
  key_results?: string[]
  end_event?: string
  chapter_start?: number
  chapter_end?: number
  stages?: OutlineStage[]
  chapters?: Array<{ seq?: number; title?: string; goal?: string; beats?: string[] }>
}

/** 整书大纲草稿请求（§11 ③：梗概 + 大致章节数 + 大致故事线 → Planner 提案） */
export interface OutlineDraftBody {
  premise: string
  chapter_count: number
  storyline: string
}

/** 整书大纲草稿响应（不落库可反复生成；LLM 失败 → outline:{} + error 降级） */
export interface OutlineDraft {
  outline: BookOutline
  error: string | null
}

/** 整书大纲（Objective → 卷 → 逐章目标；确认落库后含 premise/chapter_count/storyline） */
export interface BookOutline {
  premise?: string
  chapter_count?: number
  storyline?: string
  objective: string
  volumes: OutlineVolume[]
}

/** 整书大纲确认落库请求（PUT outline → volume_outlines 单行整体替换） */
export interface OutlineConfirmBody {
  objective: string
  volumes: Array<{
    title: string
    theme?: string
    goal: string
    key_results?: string[]
    end_event?: string
    chapter_start?: number
    chapter_end?: number
    stages?: OutlineStage[]
    chapters?: Array<{ title: string; goal: string; beats: string[] }>
  }>
  premise: string
  chapter_count: number
  storyline: string
}

/** 整书大纲读取响应（GET outline；无大纲 → outline:null 不 500） */
export interface BookOutlineResponse {
  outline: BookOutline | null
}

/** 势力（世界观浏览） */
export interface LoreFaction {
  name: string
  stance: string | null
  resources: string[]
}

/** 地点（世界观浏览） */
export interface LoreLocation {
  name: string
}

/** 世界观浏览（GET world：world_rules 含 realm_order 列表、hard_constraints + 势力/地点） */
export interface WorldView {
  world_rules: Record<string, unknown>
  hard_constraints: string[]
  factions: LoreFaction[]
  locations: LoreLocation[]
}

/** 人物卡片（静态基底 + 当前状态台账 §7.7，state 按当前章物化 {field: new_value}） */
export interface CharacterCard {
  id: string
  name: string
  race: string | null
  origin: string | null
  realm_cap: string
  personality: string | null
  base_attrs: Record<string, unknown>
  state: Record<string, string>
}

/** 设定实体（§7.11 ④ 自动建档：武器/功法/技能/地点，低风险正文抽取自动登记） */
export interface LoreEntity {
  id: string
  entity_type: 'item' | 'skill' | 'location' | (string & {})
  name: string
  description: string | null
  first_seen_chapter: number | null
}

/** 事件台账单条（§7.4 中期记忆全量：抽取节点落库后此前只写不读）。
 * participants 后端已把 canonical 人物 UUID 翻成人名，已删角色被丢弃。 */
export interface StoryEvent {
  id: string
  summary: string
  participants: string[]
  source_chapter: number
  confidence: number
}

/** 人物状态台账变更单条（§7.7 追加式：old_value → new_value @ 第 N 章）。
 * 含 valid_to 非空的已失效行——这正是 get_character_state 压平时丢掉的变化历史。 */
export interface CharacterStateChange {
  field: string
  old_value: string | null
  new_value: string | null
  chapter_seq: number
  source_chapter: number
  confidence: number
  valid_from: number | null
  valid_to: number | null
}

/** 扫榜单条（§10：外部榜单已 sanitize allowlist 字段，只当灵感参考；rank 从 1 起） */
export interface RankingItem {
  rank: number
  title: string
  author: string | null
  tags: string[]
  hot: string | null
}

/** 世界拓扑节点（GET graph → Python GraphNodeOut，§9 图谱：4 类分组全量含孤立项） */
export interface GraphNode {
  id: string
  name: string
  type: 'character' | 'faction' | 'location' | 'entity' | (string & {})
  realm_cap?: string | null
  stance?: string | null
  entity_type?: string | null
  parent_id?: string | null
}

/** 世界拓扑边（GET graph → Python GraphEdgeOut，§9 图谱：人物关系活跃/失效 + 地点层级） */
export interface GraphEdge {
  source_id: string
  target_id: string
  /** 人物关系类型（hostile/ally/…）或 hierarchy（地点层级） */
  edge_type: string
  confidence?: number | null
  /** valid_to 非空 → 已失效（前端渲染虚线） */
  expired: boolean
  source_chapter?: number | null
}

/** 世界拓扑全量（GET /projects/:pid/graph → Python WorldGraphOut，ECharts 力导向渲染） */
export interface WorldGraphResponse {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

/** 伏笔池台账单条（GET /projects/:pid/foreshadows → Python ForeshadowOut，§7.9 状态机） */
export interface Foreshadow {
  id: string
  description: string
  /** planted / developing / resolved / dropped */
  status: string
  planted_chapter: number
  resolved_chapter: number | null
  trigger: Record<string, unknown>
}

/** 扫榜响应（GET /rankings → Python RankingsOut，§10）。
 * source=remote（实时榜单）/ sample（降级样例）；error 为降级原因（网络不可达/已禁用）。
 * 数据只作建书前的题材风向灵感工具，不进记忆/事实层、不注入任何生成节点。 */
export interface RankingsResponse {
  source: string
  tool: string
  fetched_at: string | null
  error: string | null
  items: RankingItem[]
}
