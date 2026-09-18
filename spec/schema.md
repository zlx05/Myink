# Ai Ink 数据契约（JSON Schema）— Phase 0

> **用途**：定义核心对象的 JSON Schema，作为 Pydantic 强校验（plan.md §3 已确认决策）的蓝本。保证 **extract 抽取输出 / 校验器读取 / 数据库存储** 三方字段一致，杜绝阶段 1 编码期返工。
>
> **版本**：v0.1（2026-08-05，Phase 0 草案）。落地为 Pydantic model 时以本文件为准，字段增删需同步本文档 + plan.md §6.4 / §11。

## 约定

- 所有业务表带 `project_id`（多租户双键硬隔离，plan.md §14.1）；
- 所有事实 / 事件 / 关系 / 状态带 `source_chapter` + `confidence`（证据链可追溯，plan.md §8.7）；
- 时间窗字段：`valid_from` / `valid_to`（`null` = 当前有效），支撑状态翻转与"已失效"判定；
- 枚举字段（`field`、`conflict_type`、`severity`、`status` 等）与 plan.md §6.4 / §7 / §8 一致。

---

## 1. CharacterState — 人物状态台账一行（追加式）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CharacterState",
  "type": "object",
  "required": ["project_id", "character_id", "chapter_seq", "field", "new_value", "source_chapter", "confidence"],
  "properties": {
    "id":              { "type": "string", "format": "uuid" },
    "project_id":      { "type": "string", "format": "uuid" },
    "character_id":    { "type": "string", "format": "uuid" },
    "chapter_seq":     { "type": "integer", "minimum": 1 },
    "field":           { "type": "string", "enum": ["location","injury","realm","power","item","knowledge","goal","identity","alive"] },
    "old_value":       { "type": "string" },
    "new_value":       { "type": "string" },
    "source_chapter":  { "type": "integer", "minimum": 1 },
    "confidence":      { "type": "number", "minimum": 0, "maximum": 1 },
    "valid_from":      { "type": "integer", "minimum": 1 },
    "valid_to":        { "type": ["integer","null"], "minimum": 1 }
  }
}
```

- **追加式，只追加不覆盖**（plan.md §7.7）：当前状态 = 按 `chapter_seq` 最近一条有效记录（查询时物化）；
- `alive` 作为独立字段，死亡 / 复活显式记录，防"死而复生"类错误。

## 2. Character — 人物静态基底（长期事实）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Character",
  "type": "object",
  "required": ["project_id", "name", "realm_cap"],
  "properties": {
    "project_id":     { "type": "string", "format": "uuid" },
    "name":           { "type": "string" },
    "aliases":        { "type": "array", "items": { "type": "string" } },
    "race":           { "type": "string" },
    "origin":         { "type": "string", "description": "出身" },
    "realm_cap":      { "type": "string", "description": "境界上限（战力硬约束）" },
    "personality":    { "type": "string", "description": "性格基调/目标（人设漂移审计基线，plan.md §8.6）" },
    "base_attrs":     { "type": "object", "description": "JSONB 基础属性" },
    "version":        { "type": "integer", "description": "乐观版本号（plan.md §7.6）" }
  }
}
```

- 只存**不易变**信息；易变状态一律进 `character_states`（plan.md §7.7）。

## 3. ChapterPlan — 章节计划（规划 agent 输出）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ChapterPlan",
  "type": "object",
  "required": ["goals", "scenes", "characters", "expected_events", "hard_constraints"],
  "properties": {
    "project_id":      { "type": "string", "format": "uuid" },
    "chapter_seq":     { "type": "integer", "minimum": 1 },
    "goals":           { "type": "array", "items": { "type": "string" }, "description": "推进主线/支线" },
    "scenes":          { "type": "array", "items": { "$ref": "#/definitions/Scene" } },
    "characters":      { "type": "array", "items": { "$ref": "#/definitions/CharacterPresence" } },
    "hooks_to_plant":  { "type": "array", "items": { "type": "string" }, "description": "本章要种的伏笔" },
    "hooks_to_resolve":{ "type": "array", "items": { "type": "string" }, "description": "须命中池中开放伏笔（plan.md §7.9）" },
    "expected_events": { "type": "array", "items": { "type": "string" }, "description": "预期事件（大纲-正文偏差比对输入，plan.md §8.6）" },
    "hard_constraints":{ "type": "array", "items": { "type": "string" }, "description": "本章必须遵守的硬约束" },
    "transition": { "$ref": "#/definitions/ChapterTransition", "description": "新规划的跨章接续方案；旧计划可省略" }
  },
  "definitions": {
    "ChapterTransition": {
      "type": "object",
      "required": ["mode", "anchor_quote", "pending_action", "opening_beat", "bridge"],
      "properties": {
        "mode": { "enum": ["continue", "time_jump", "scene_cut", "opening"] },
        "anchor_quote": { "type": "string", "description": "前章章尾逐字引文；无原文则空" },
        "pending_action": { "type": "string", "description": "待回应动作/问题/危险" },
        "opening_beat": { "type": "string", "minLength": 1, "description": "本章第一拍的新进展" },
        "bridge": { "type": "string", "description": "转场线索及原悬念如何衔接" }
      }
    },
    "Scene": {
      "type": "object",
      "required": ["location_id", "participants", "goal"],
      "properties": {
        "location_id":   { "type": "string", "description": "地点名（规划产物是 LLM 直接输出，只认识名字不持库内 id；id 归一化发生在落库候选 §7.5）" },
        "participants":  { "type": "array", "items": { "type": "string" }, "description": "人物名列表" },
        "goal":          { "type": "string" },
        "time":          { "type": "string", "description": "场景时间点（多线对齐用）" }
      }
    },
    "CharacterPresence": {
      "type": "object",
      "required": ["character_id", "expected_state"],
      "properties": {
        "character_id":   { "type": "string", "description": "人物名（非 UUID，同 Scene.location_id 理由）" },
        "expected_state": { "type": "object", "description": "从状态台账取的要求状态（位置/伤势/境界）" }
      }
    }
  }
}
```

## 4. Event — 剧情事件（中期记忆）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Event",
  "type": "object",
  "required": ["project_id", "summary", "participants", "source_chapter", "confidence"],
  "properties": {
    "id":              { "type": "string", "format": "uuid" },
    "project_id":      { "type": "string", "format": "uuid" },
    "summary":         { "type": "string" },
    "participants":    { "type": "array", "items": { "type": "string", "format": "uuid" }, "description": "必须归一为 canonical id（plan.md §7.5）" },
    "location_id":     { "type": "string", "format": "uuid" },
    "timeline":        { "type": "string", "description": "剧情内时间（相对/绝对）" },
    "related_threads": { "type": "array", "items": { "type": "string", "format": "uuid" }, "description": "关联剧情线" },
    "source_chapter":  { "type": "integer", "minimum": 1 },
    "confidence":      { "type": "number", "minimum": 0, "maximum": 1 },
    "promoted_to_fact":{ "type": "boolean", "default": false, "description": "事件→事实升格（plan.md §7.3）" },
    "version":         { "type": "integer" }
  }
}
```

## 5. Fact — 长期事实

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Fact",
  "type": "object",
  "required": ["project_id", "content", "source_chapter", "confidence"],
  "properties": {
    "id":              { "type": "string", "format": "uuid" },
    "project_id":      { "type": "string", "format": "uuid" },
    "content":         { "type": "string" },
    "category":        { "type": "string", "description": "世界观/身份/归属/关系/规则" },
    "is_hard":         { "type": "boolean", "description": "是否硬约束（恒在 Top-K，plan.md §7.2）" },
    "source_chapter":  { "type": "integer", "minimum": 1 },
    "confidence":      { "type": "number", "minimum": 0, "maximum": 1 },
    "confirm_status":  { "type": "string", "enum": ["pending","confirmed","rejected","expired"] },
    "valid_from":      { "type": "integer", "minimum": 1 },
    "valid_to":        { "type": ["integer","null"], "minimum": 1 },
    "version":         { "type": "integer" }
  }
}
```

## 6. Relation — 人物/势力关系（带时间窗）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Relation",
  "type": "object",
  "required": ["project_id", "source_id", "relation_type", "target_id", "source_chapter", "confidence"],
  "properties": {
    "id":             { "type": "string", "format": "uuid" },
    "project_id":     { "type": "string", "format": "uuid" },
    "source_id":      { "type": "string", "format": "uuid" },
    "relation_type":  { "type": "string", "enum": ["hostile","ally","master_student","located_in","owns","defeated_by","knows","promises","happened_at"] },
    "target_id":      { "type": "string", "format": "uuid" },
    "properties":     { "type": "object", "description": "强度/条件" },
    "confidence":     { "type": "number", "minimum": 0, "maximum": 1 },
    "source_chapter": { "type": "integer", "minimum": 1 },
    "version":        { "type": "integer" },
    "valid_from":     { "type": "integer", "minimum": 1 },
    "valid_to":       { "type": ["integer","null"], "minimum": 1 }
  }
}
```

- 当前关系 = 最新一条 `valid_to IS NULL` 的记录（plan.md §7.8）；
- 敌对/盟友为双向语义，写入时成对落库或查询时双向检查（plan.md §9.3）。

## 7. Foreshadow — 伏笔（状态机）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Foreshadow",
  "type": "object",
  "required": ["project_id", "description", "status", "planted_chapter", "trigger", "related_entities"],
  "properties": {
    "id":               { "type": "string", "format": "uuid" },
    "project_id":       { "type": "string", "format": "uuid" },
    "description":      { "type": "string" },
    "status":           { "type": "string", "enum": ["planted","developing","resolved","dropped"] },
    "planted_chapter":  { "type": "integer", "minimum": 1 },
    "resolved_chapter": { "type": ["integer","null"], "minimum": 1 },
    "trigger":          { "type": "object", "description": "回收条件（结构化）：触发者 + 动作（获得/知道/遭遇）+ 对象（plan.md §7.9）" },
    "related_entities": { "type": "array", "items": { "type": "string", "format": "uuid" } },
    "last_touched":     { "type": "integer", "minimum": 1, "description": "最近推进章节（回收压力计算）" }
  }
}
```

## 8. PlotThread — 剧情线

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PlotThread",
  "type": "object",
  "required": ["project_id", "name", "kind", "status", "priority"],
  "properties": {
    "id":              { "type": "string", "format": "uuid" },
    "project_id":      { "type": "string", "format": "uuid" },
    "name":            { "type": "string" },
    "kind":            { "type": "string", "enum": ["main","side"] },
    "status":          { "type": "string", "enum": ["active","stalled","closed"] },
    "priority":        { "type": "integer", "minimum": 1, "description": "线程债务治理，plan.md §8.6" },
    "progress":        { "type": "string" },
    "participants":    { "type": "array", "items": { "type": "string", "format": "uuid" } },
    "last_progress_chapter": { "type": "integer", "minimum": 1 },
    "open_duration":   { "type": "integer", "description": "开放未推进时长（章节数）" }
  }
}
```

## 9. Finding — 校验发现（L1/L2 统一）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Finding",
  "type": "object",
  "required": ["conflict_key", "conflict_type", "severity", "evidence", "suggestion"],
  "properties": {
    "finding_id":    { "type": "string" },
    "conflict_key":  { "type": "string", "description": "hash(类型+实体+位置)，跨修订轮稳定（plan.md §6.4）" },
    "conflict_type": { "type": "string", "enum": ["faction","power","timeline","location","character","character_state","relation","foreshadow","item_rule","plotline","persona","style"] },
    "severity":      { "type": "string", "enum": ["critical","major","minor","hint"] },
    "scope":         { "type": "string", "enum": ["local","structural"], "description": "冲突作用域，修订分级（plan.md §6.5）" },
    "source":        { "type": "string", "enum": ["L1","L2"], "description": "L1 确定性规则 / L2 语义——全局审计抽样维度（persona/style，2026-08-12/13）+ 单章点级正文-台账语义比对（validator_l2，2026-08-13，§8.6 落地注记）" },
    "evidence":      { "type": "array", "items": { "type": "object", "properties": { "chapter": { "type": "integer" }, "quote": { "type": "string" } }, "required": ["chapter","quote"] } },
    "confidence":    { "type": "number", "minimum": 0, "maximum": 1, "description": "仅 L2 需要" },
    "suggestion":    { "type": "string" }
  }
}
```

> **新增冲突类型**（相对 plan.md §8.4 扩展）：`plotline`（剧情线停滞/线程债务）、`persona`（人设漂移）、`style`（文风/AI 味/桥段重复）——对应 §8.6 长线一致性治理。

## 10. MutationCandidate — 记忆候选（待确认池）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MutationCandidate",
  "type": "object",
  "required": ["kind", "project_id", "source_chapter", "payload"],
  "properties": {
    "id":            { "type": "string", "format": "uuid" },
    "kind":          { "type": "string", "enum": ["event","fact","character_state","relation_change","foreshadow","chapter_summary","memory_removal"] },
    "project_id":    { "type": "string", "format": "uuid" },
    "source_chapter":{ "type": "integer", "minimum": 1 },
    "payload":       { "type": "object", "description": "对应 kind 的对象体（Event/Fact/CharacterState/Relation/Foreshadow/摘要；character_state/relation_change 候选带 old_value/new_value——old_value 由 extract 按注入快照校准（§8.6 正文-台账语义比对前置）；memory_removal 为 {memory_type, memory_id, display}，阶段 3 编辑校正删除候选）" },
    "confidence":    { "type": "number", "minimum": 0, "maximum": 1 },
    "status":        { "type": "string", "enum": ["pending","confirmed","rejected"], "default": "pending" }
  }
}
```

- 存 **DB 表**（`memory_candidates`），Redis 只做短期标记（plan.md §5.3 / §7.3）。
- `relation_change` 候选 payload（§6 Relation 的候选形态扩展，2026-08-13 落地）：`{source_id: 人物名, target_id: 人物名, relation_type: 白名单枚举（hostile/ally/master_student/located_in/owns/defeated_by/knows/promises/happened_at）, old_value: 注入快照中的当前关系类型, new_value: 目标类型, source_chapter, confidence}`——extract 只抽「正文**明确发生**的关系演变」，`old_value` 须与注入的当前台账快照一致（`extract_messages` context 注入「【当前台账快照】」）；落库由 persist 先关闭同 (source,target) 有序对全部活跃旧行。这是样例 17/22 正文-台账语义比对 L2 与 L1 窄脚印（old≠台账）的机制入口（plan.md §8.6 / spec/conflict-samples.md）。

## 11. ValidationReport — 校验报告

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ValidationReport",
  "type": "object",
  "required": ["project_id", "chapter_seq", "findings", "summary"],
  "properties": {
    "project_id":  { "type": "string", "format": "uuid" },
    "chapter_seq": { "type": "integer", "minimum": 1 },
    "findings":    { "type": "array", "items": { "$ref": "finding.schema.json" } },
    "summary":     { "type": "object", "properties": {
                       "total": { "type": "integer" },
                       "critical": { "type": "integer" },
                       "resolved": { "type": "integer" }
                    } }
  }
}
```

## 12. RetrievedContext — 召回上下文（recall 节点输出）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "RetrievedContext",
  "type": "object",
  "required": ["long_term_facts", "mid_term_events", "short_context", "entity_snapshots", "token_usage"],
  "properties": {
    "long_term_facts":  { "type": "array", "items": { "type": "object", "properties": { "fact_id": { "type": "string" }, "source_chapter": { "type": "integer" } } } },
    "mid_term_events":  { "type": "array", "items": { "type": "object", "properties": { "event_id": { "type": "string" }, "chapter": { "type": "integer" }, "confidence": { "type": "number" }, "recalled_by": { "type": "string", "description": "混合召回标签：vector / keyword / vector+keyword（仅混合召回补充的事件有）" } } } },
    "short_context":    { "type": "array", "items": { "type": "object" }, "description": "上一章摘要 + 上一章候选事件 + 本章开头 + 最近场景（plan.md §7.1）" },
    "recent_openings":  { "type": "array", "items": { "type": "object" }, "description": "最近 4 个历史章开头 [{chapter, text}]；仅供差异化比较，不作接续位置" },
    "entity_snapshots": { "type": "array", "items": { "type": "object" }, "description": "人物/势力/地点当前状态快照（台账最新）：character_id / name / realm_cap / state / state_changes / personality / relations；state_changes 为 {field: {old, chapter}}，只收旧值非空且与新值不同的真实跃迁（否则会把没变的字段也渲染成一堆伪变化）" },
    "setting_snapshots":{ "type": "array", "items": { "type": "object" }, "description": "设定实体快照（§7.11 ④ 物品/功法/地点，entities 表）：entity_id / entity_type / name / description / first_seen_chapter；按本章场景地点名相关度取样，上限 12 条。注入 plan/write/audit，**不注入 extract**——extract 的台账快照块用于校准 character_state.old_value 与 relation_change 候选，掺入非人物行会招来假候选" },
    "reflexions":       { "type": "array", "items": { "type": "object" }, "description": "在效写作经验（§8.9 reflexion）：content / lesson_type / category / source_chapter；上限 8 条" },
    "token_usage":      { "type": "integer" },
    "recall_stats":     { "type": "object", "description": "事件混合召回占比（§7.2/§16，2026-08-12 落地）：{ vector_hits, keyword_hits, fused_total, recall_tokens_est, context_tokens_est, share }；无混合召回时为空对象 {}" }
  }
}
```

---

## 13. WritingLesson — 跨章写作经验（reflexion 产物，§8.9）

```json
{
  "project_id": "uuid",
  "category": "power|faction|timeline|location|character|character_state|relation|foreshadow|item_rule|plotline|persona|style",
  "lesson_type": "planning|writing|both",
  "content": "跨章可复用的一句话写作经验（注入后续章节规划/写作）",
  "content_hash": "sha256(content)",
  "evidence": [ { "chapter": 3, "conflict_type": "power", "severity": "major", "quote": "原文片段" } ],
  "confidence": 0.9,
  "source_chapter": 1,
  "source_batch_task_id": "uuid",
  "status": "proposed|active|rejected",
  "recurrence_count": 0,
  "last_recurrence_at": null
}
```

- 生命周期 `proposed → active / rejected`（去掉 superseded：同 category 演化是 update 同一行，无新盖旧）；
- **分级**：源自 critical/major finding → `proposed` 候选池待人工确认；minor/hint → `active` 自动生效；
- **演化**：同 category 已有行 → update 同一行（content 换演化版、evidence 追加、confidence 更新、保留 id、继承复发指标）；无 → create；
- **去重**：`(project_id, content_hash) WHERE status='active'` 部分唯一约束 + `_batch_already_reflexed` 批次 guard + LLM 语义去重；
- 编排层写库（reflexion 节点），Agent 不直写；人工确认经确认 API（`confirm` / `reject`）。

---

## 14. GlobalAuditReport — 全局审计报告（§8.6 抽样 L2 产物，2026-08-12 人设漂移 + 2026-08-13 桥段重复 / 文风漂移三维度落地）

```json
{
  "project_id": "uuid",
  "window_start": 1,
  "window_end": 10,
  "audited_up_to_chapter": 10,
  "trigger": "batch|manual",
  "source_batch_task_id": "uuid|null",
  "status": "completed|failed",
  "sampled_characters": [ { "character_id": "uuid", "name": "林砚" } ],
  "findings": [ ],
  "summary": { "sampled": 1, "findings": 1, "chapters": 10,
               "bridge": { "pairs": 1, "findings": 1 } },
  "error": null
}
```

- **触发**：批次收尾 `global_audit` 节点每 K 章（`AUDIT_INTERVAL` 默认 10，窗口 = [上次审计后 +1, 当前最大章]，长度 ≥ K 才触发）或手动端点 `POST .../projects/{pid}/global-audit`（显式动作不做 K 门槛）；below_threshold 短路零成本、不落行；
- **marker**：`audited_up_to_chapter` = window_end 作跨批进度标记（`last_audited_up_to` 读 max）——LLM/解析失败也落 `status=failed` 行并推进（非阻塞 + 有界，防每批重审同一毒窗口）；
- **findings 复用 §8 Finding 形状**：一次审计可混排三个维度的 finding——`conflict_type=persona`（`persona:{角色}:{章}`，切片 1）/ `conflict_type=style`（`bridge:{历史事件id}:{章}` 与 L1 桥段同前缀，切片 2；`style_drift:{章}` 文风漂移，切片 3），均 `severity=hint, scope=local, source=L2, evidence=[{chapter,quote}]`，前端审计视图与章节 finding 同构渲染；
- **三维度编排**：`run_global_audit` 单次调用跑人设漂移 + 桥段重复 + 文风漂移（共用窗口 / marker / 报告行）；`summary["bridge"]={"pairs","findings"}` 桥段维度跑了才有、`summary["style"]={"sampled","findings"}` 文风维度采样非空才有；维度失败 error 记 `summary["errors"]`（报告 status=completed 当任一维度成功或全维度中性，failed 当有维度失败且无维度成功；error 字段单失败透传原文 / 多失败 k=v 拼接）；
- **0 误报**：共享 `_verify_findings` 确定性守卫（evidence 引文必须是该章正文逐字子串 / chapter 在窗口 / 实体在采样集 / kind 合法 / 置信度 ≥0.6，丢弃其余）——persona 经 `normalize_and_verify_findings`、bridge 经 `normalize_and_verify_bridge_findings`、style 经 `normalize_and_verify_style_findings` 包装，非信任 LLM；
- **RLS**：租户业务表（带 project_id），`enable_row_level_security` 全表迭代自动覆盖（**不在** `_NO_RLS_TABLES`，仅 agent_runs/tasks 豁免）。

---

## 与实现的关系

- 阶段 1 用 Pydantic `BaseModel` 直接映射上表（`Field` 约束与 Schema 一致），LangGraph 节点间传结构化对象（plan.md §6.4 `ChapterState` TypedDict）；
- Schema 是 extract / plan_chapter / validate 三方输出的唯一契约：**坏数据拒绝但不崩**（plan.md §3 已确认决策）；
- 新增字段（如 `plotline` / `persona` / `style` 冲突类型）已同步 plan.md §8.6 / §8.4，后续一致演进。

### 候选评审元数据（2026-09-08）

`memory_candidates.review` 为可空 JSON：`mode=revise|memory_only`、`reason`、`applied`；旧稿候选作废记录 `superseded=true`。旧记录 NULL 保持原有仅拒绝记忆语义。拒绝默认 revise，恢复评审时修订正文并重新抽取、校验和审核。
