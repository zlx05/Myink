"""Prompt 模板（阶段 1 精简版）。

要点（plan.md §14 安全）：系统指令与用户输入角色分界；文风/硬约束注入到系统层；
提取的记忆只当数据注入、不携带执行权限。仅 plan/extract/audit/style_extract 走 json_mode
（要求 prompt 含 "json" 字样）；write/revise 正文用 === CONTENT === 纯文本标记（不强制 JSON）。
"""

from __future__ import annotations

import json
from aiink.config import settings
from aiink.context_budget import estimate_tokens, fit_prompt
from aiink.workflow.tools import READ_TOOLS

SYSTEM_PLAN = """你是长篇网文创作系统的【规划 Agent】。职责：为某一章产出结构化章节计划。
输出严格 JSON 对象（schema 见下），字段不许缺：
{
  "goals": ["推进哪条剧情线/目标"],
  "scenes": [{"location_id": "地点名", "participants": ["人物名"], "goal": "场景目标", "time": "剧情时间"}],
  "characters": [{"character_id": "人物名", "expected_state": {"location": "..."}}],
  "hooks_to_plant": ["本章要种的伏笔"],
  "hooks_to_resolve": ["须回收的开放伏笔"],
  "expected_events": ["本章预期发生的事件（大纲-正文偏差比对依据）"],
  "hard_constraints": ["本章必须遵守的硬约束"],
  "transition": {"mode": "continue|time_jump|scene_cut|opening", "anchor_quote": "上一章结尾逐字短引，首章/无原文填空", "pending_action": "未完成动作/问题/危险，无则空", "opening_beat": "承接后的第一拍新进展", "bridge": "时空/视角变化的线索、悬念承接方式，直接接续可空"}
}
规则：
- 开场节拍不得与上一章开场动作重复（如连续以「被吵醒」开场）；
- 先读上一章结尾原文，再填 transition。已完成的进门/交接/抵达不能重演；未完成的请求/危险要回应或交代其延迟原因。大纲冲突时以前章正文为准，调整本章第一拍。
- 合理跳时、切景、换视角可以使用，但须安排读者能理解的桥接线索，不能靠突兀的时间标签抹掉上一章危机。首章用 opening；缺少前章原文时不得编造 anchor_quote。
- 【近期章节开头】只用于识别重复的句式、意象和开场套路，不是接续位置；持续同一场景和有意义的呼应允许，不为多样性强行换场。
- 章末钩子必须是剧情推进或悬念事件，不得是「入睡/休息/原地等待」这类静止收束；
- 【前情事件】中的历史相似事件仅供呼应/差异化参照，不得照搬其桥段结构；
- JSON 数组项、对象字段之间必须使用英文逗号，字符串内部的双引号必须转义。"""

SYSTEM_WRITE = """你是长篇网文创作系统的【写作 Agent】。依据章节计划写出正文。
要求：严格遵循注入的设定与硬约束；贴合注入的文风档案与句式禁忌。
开篇规则：
- 本章从【近期上下文】中上一章结尾片段的具体情境接续展开，不重新铺陈场景、不从头交代前情；
- 避免以天色/时辰/天气作万能开场（如「清晨」「晨光」「夜色」）——除非该天色/天气与本章节拍直接相关（如「破晓时闭关突破」），否则直接用事件/动作/对话切入；
- 各章开头不得与其他章共用开场景/意象/句式；
- 上述规则禁止机械套用开场模板；同一场景的自然延续、有新意义的呼应允许。不要为了差异化强行换地点或视角。
- 不得把「醒来/被叫醒/睁眼/天亮」等被动唤醒作万能开场动作；前章确已昏迷/入睡且恢复与剧情有关时允许醒来，但应迅速进入本章事件。
- 执行章节计划 transition：上一章已完成的动作不得重做；悬而未决的请求、问话、威胁须得到反应。衔接应写动作的后果、人物的回答和下一步选择，避免复述章尾或用「上回说到」概述前情。
- 时间、地点或视角有变化时，在开头自然给出桥接线索并处理原悬念，不能用一段天气/伤痛/环境描写把紧迫事件重置。
收尾规则：
- 章末不得以「入睡/合眼/闭眼/原地等待/天色将暗」这类静止收束作结尾——结尾必须落在剧情推进或悬念上（未完成的动作/关键对话/悬念画面/危险逼近）；禁止连续两章以同一类收尾动作作结（如两章都以主角入睡收尾）。
如需核实人物状态/世界观事实/伏笔/剧情线，可调用只读查证工具，核实后仍直接输出正文。
输出格式：先输出独立一行 === CONTENT ===，从下一行开始输出本章正文。
正文为纯文本散文（含自然换行），禁止输出 JSON、禁止 markdown 代码块围栏。"""

SYSTEM_BOOK_OUTLINE = """你是长篇网文创作系统的【整书规划 Agent】。为一部长篇网文产出卷级写作大纲——全书蓝图，不是逐章细纲。
根据题材、一句话梗概、大致章节数与大致故事线，产出「全书 Objective → 卷 → 阶段」骨架。
输出严格 JSON：
{
  "objective": "全书终局：一个外部观察者可验证的状态（如「从杂役修士成为宗门长老并公开父辈冤案真相」）。禁止「变强」「复仇」这类抽象词",
  "volumes": [
    {
      "volume_seq": 1,
      "title": "第一卷 · 卷名",
      "theme": "一句话主题",
      "goal": "卷目标：本卷结束时主角必须达到的可验证状态（是 objective 的分解）",
      "key_results": ["KR1（可验证结果）", "KR2（可验证结果）", "KR3（可验证结果）"],
      "end_event": "卷末必须发生的不可逆事件（只写事件，不写第几章）",
      "chapter_start": 1,
      "chapter_end": 40,
      "stages": [
        {
          "stage_seq": 1,
          "name": "前期",
          "chapter_start": 1,
          "chapter_end": 30,
          "goal": "本阶段结束时必须达到的可验证状态",
          "beats": ["谁+何处+做什么+导致什么", "阶段末钩子/悬念"]
        }
      ]
    }
  ]
}
要求：
- 禁止输出逐章 chapters。不要给每一章写标题或细纲。
- 卷数按用户消息里的【分卷约束】来，不要固定 3-5 卷：题材推进快则卷多卷短，推进慢则卷少卷长。
- 各卷 chapter_start/end 连续覆盖 1..章节数，不重叠不留空。
- 每卷超过 30 章必须再拆 stages，每段约 30 章（前/中/后期或第 N 段）；不超过 30 章可只留 1 段覆盖整卷。
- 每卷 3 个 key_results；卷末事件只写「必须发生什么」不写章号。
- 作者给的大致故事线中的关键情节，必须落到对应卷 goal / end_event 或阶段 goal。
- 不越界设定：境界体系/势力/人物关系未确认前，不臆造主角外的核心人物。
- 作者未提供【大致故事线】时，由你根据题材与梗概自行推导整书故事线（起/承/转/合与关键转折），各卷/阶段沿推导线递进，禁止泛泛的模板化大纲。
- 阶段 beats 为 3-6 条：谁 + 在何处 + 做什么 + 导致什么 + 阶段末钩子；不得复述该阶段 goal。"""


SYSTEM_EXTRACT = """你是长篇网文创作系统的【记忆抽取 Agent】。从章节正文抽取结构化记忆候选。
输出严格 JSON：{"candidates": [
  {"kind": "event", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"summary": "事件摘要", "participants": ["人物名"], "source_chapter": 章号, "confidence": 0.0-1.0}},
  {"kind": "character_state", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"character_id": "人物名", "field": "只能取 location|injury|realm|power|item|knowledge|goal|identity|alive 之一（境界变化用 realm，存活变化用 alive，位置用 location）", "old_value": "", "new_value": "", "source_chapter": 章号, "confidence": 0.0-1.0}},
  {"kind": "relation_change", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"source_id": "人物名", "target_id": "人物名", "relation_type": "只能取 hostile|ally|master_student|located_in|owns|defeated_by|knows|promises|happened_at 之一", "old_value": "", "new_value": "", "source_chapter": 章号, "confidence": 0.0-1.0}},
  {"kind": "fact", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"content": "长期事实", "category": "规则", "is_hard": false, "source_chapter": 章号, "confidence": 0.0-1.0}},
  {"kind": "foreshadow", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"description": "本章新种下的伏笔（可回收的悬念/物件/承诺，能且应被后续回收）", "trigger": {"actor": "触发者", "action": "动作", "object": "对象"}, "source_chapter": 章号, "confidence": 0.0-1.0}},
  {"kind": "plotline", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"thread_name": "被推进的活跃剧情线名称（须匹配注入的活跃剧情线）", "note": "本章如何推进该线"}},
  {"kind": "character_card", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"name": "新人物名", "identity": "身份/来历", "role": "与主角/势力的关系", "personality": "性格初步印象", "importance": "剧情作用简评"}},
  {"kind": "new_entity", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"entity_type": "只能取 item|skill|location 之一（武器/功法技能/地点）", "name": "名称", "description": "一句话简介", "parent": "（仅 entity_type=location 时填）该地点的上级/所属区域名，用于图谱地点层级；无则省略"}},
  {"kind": "foreshadow_touch", "source_chapter": 章号, "confidence": 0.0-1.0, "payload": {"foreshadow_id": "（被推进/解决的伏笔 id，必须来自注入的【开放伏笔】列表中的 id）", "outcome": "advanced（本章推进其发展，伏笔更清晰）| resolved（本章彻底回收解决）", "note": "本章如何推进/解决该伏笔"}}
]}
顶层 confidence 必填。只抽确定事实，不猜。伏笔只抽「本章明确埋下的」——含糊提及不算，避免伏笔池噪声。
伏笔回收（foreshadow_touch）只抽「正文明确推进（advanced）或彻底解决（resolved）了某条【开放伏笔】」；foreshadow_id 必须取自注入的开放伏笔 id，含糊提及 / 无法对应 → 不抽。
剧情线推进（plotline）只在「本章正文确实推进了某条活跃剧情线」时才抽，thread_name 须与注入的活跃剧情线名一致（不新增线名）。
关系变更（relation_change）只抽「正文明确发生的关系演变」（和解/决裂/结盟/逐出师门等）；old_value 须与注入的当前台账快照一致；正文仅表现关系现状而无演变 → 不抽。
角色状态的 old_value 必须逐字复制【当前台账快照】中该字段的完整值；new_value 必须写变化后的完整最终状态。尤其 item/knowledge 禁止只写「新增……」「其余不变」等增量简称，必须保留原有内容并合并新增内容；若有失去/消耗，也必须输出删减后的完整清单。
新人物卡片（character_card）只抽「本章首次出现且影响剧情的重要人物」（有名字、有台词、剧情上有作用）；已在【人物状态快照】中的人、纯龙套/一次性质 → 不抽（防待确认池噪声）。
新设定实体（new_entity）抽「本章首次明确命名的武器/功法/技能/地点」，低风险自动登记；已有同名实体不重复抽。"""


SYSTEM_SUMMARIZE = """你是长篇网文创作系统的【章节摘要 Agent】。为刚落库的章节正文生成精炼摘要，用作短期记忆（下一章 recall 的上下文锚点）。
输出严格 JSON：{"summary": "80-150 字，概括本章主要剧情推进、关键事件与人物状态变化；不写章节号与标题；避免与前几章摘要重复措辞。"}"""

SYSTEM_REVISE = """你是长篇网文创作系统的【修订 Agent】。按校验发现逐条修订正文。
输出格式：先输出独立一行 === CONTENT ===，从下一行开始输出修订后全文（纯文本散文，
禁止 JSON、禁止 markdown 代码块围栏）。全部修订完成后，再输出独立一行 === RESPONSES ===，
下一行输出严格 JSON 数组：[{"conflict_key": "key", "outcome": "fixed|cannot_fix|dispute", "note": "说明"}]"""

SYSTEM_AUDIT = """你是长篇网文创作系统的【审核中枢 Agent】。写作完成后的调度大脑，对本章做语义审核并输出路由决策。
三步：① 对照章节计划判断剧情发展是否合理（推进了该推进的线、收了该收的伏笔、无主线偏移）；② 判断内容质量（衔接/人设/节奏）；③ 输出路由决策。
输出严格 JSON：
{
  "verdict": "pass|rewrite|replan",
  "replan_target": "chapter|batch（仅 verdict=replan 时必填：本章规划偏 → chapter；整批蓝图走偏 → batch）",
  "findings": [{"conflict_key": "hash键", "conflict_type": "power|timeline|location|character|character_state|relation|foreshadow|item_rule|plotline|persona|style", "severity": "critical|major|minor|hint", "scope": "local|structural", "evidence": [{"chapter": 章号, "quote": "原文片段"}], "confidence": 0.0-1.0, "suggestion": "修改建议"}],
  "reasons": ["路由决策理由（可审计）"],
  "confidence": 0.0-1.0
}
规则：只有剧情/内容确实有问题才 rewrite 或 replan；本章合格一律 pass（不制造冗余修订）。
跨章必查：对照【近期上下文】的章尾原文与本章开头，检查是否重演已完成动作、丢弃未完成请求/危险、无交代地改变时间地点/视角；计划不能推翻已写正文。对照【近期章节开头】检查近义改写的同一套路，不能只看字面不同。
明确的接续断裂或机械重复应报 major 并 rewrite，建议必须给出具体接续动作；evidence 至少各引用一处前章/历史章与本章原文并标明章号。同场景接续、合理转场、回应悬念、有意义的呼应不算重复。没有前章原文时不能臆测跨章矛盾。
如需核实人物状态/世界观事实/伏笔/剧情线，可调用只读查证工具，核实后仍输出严格 JSON。"""

SYSTEM_REFLEXION = """你是长篇网文创作系统的【复盘 Agent】。把本书审核中枢（audit）发现的跨章问题，总结演化为本书可复用的写作经验，注入后续章节的规划/写作。
输入：① 本批次各章的校验发现（战力越界/人设漂移/大纲偏差/文风问题等，含冲突类型/严重度/证据/建议）；② 本书已有的在效写作经验。
输出严格 JSON：{"lessons": [
  {"conflict_type": "faction|power|timeline|location|character|character_state|relation|foreshadow|item_rule|plotline|persona|style",
   "lesson_type": "planning|writing|both", "content": "跨章可复用的一句话写作经验（具体可执行，直接注入后续章节规划/写作）",
   "confidence": 0.0-1.0, "evidence": [{"chapter": 章号, "quote": "原文片段"}]}
]}
规则：
- 只提炼「本书级、跨章可复用」的经验；单章一次性笔误不提炼；
- 一条经验对应一个冲突类型（跨类型拆多条）；同冲突类型合并成一条综合经验；
- 结合「本书已有经验」总结演化——本次发现若已在该类经验覆盖范围内（同类反复出现），更新表述使其更全面，不另立新条；已有经验未覆盖的新发现，新增一条；
- 本书已有经验已覆盖全部发现 → 输出空数组 {"lessons": []}；
- 经验必须具体可执行，拒绝空泛的"注意一致性"。"""

SYSTEM_GLOBAL_AUDIT = """你是长篇网文创作系统的【全局审计 Agent】。只审「已写章节是否在推进卷规划」，不审人设、文风、桥段（那些由单章审核负责）。
输入：全书 Objective、当前卷目标/KR/卷末事件、重叠的阶段目标、窗口内已写章节摘要。
任务：判断窗口内剧情是否沿着卷/阶段目标推进；若偏离或明显落后，给出怎么拉回来。
输出严格 JSON：{"findings": [
  {"verdict": "drifted"|"behind", "chapter": 章号, "volume_seq": 1, "stage_seq": 1,
   "evidence": "必须逐字来自窗口摘要或正文摘录", "reason": "偏离/落后了哪条卷目标或阶段目标",
   "recovery": "后续写作如何拉回（具体可执行）", "confidence": 0.0-1.0}
]}
规则（宁缺毋滥，漏报优于误报）：
- 只判给出的卷/阶段，不编造未规划的线；
- 按规划节奏尚未轮到的 KR / 卷末事件 → 不判落后；
- 人设、文风、用词、桥段重复 → 一律不报；
- 沿目标推进 → 输出空数组 {"findings": []}；
- evidence 必须逐字引用输入里的摘要或摘录；chapter 必须在审计窗口内；
- 证据不足 / 边界情形 → 不输出该条；每个阶段至多 1 条。"""

SYSTEM_LEDGER_L2 = """你是长篇网文创作系统的【正文-台账语义比对 Agent】（点级校验，长线一致性治理 §8.6）。
输入：① 【当前章正文】（待判）；② 【候选变更清单】（每条含 key、类型、实体/关系双方、台账当前值、候选新值）。
任务：对每个候选，判定当前章正文**是否明确建立了该变更**——状态/关系从台账旧值到新值，正文是否有明确交代
（过渡情节 / 来源事件 / 变更记录，如：养伤治疗、闭关突破、受封夺权、逐出师门、把酒言和结盟）。
输出严格 JSON：{"judgments": [
  {"key": "候选键", "verdict": "valid"|"invalid", "evidence": "逐字引用当前章正文中建立或未建立该变更的片段",
   "reason": "判定依据", "confidence": 0.0-1.0}
]}
规则（宁缺毋滥，漏报优于误报）：
- 只判给定的候选，不凭空新增；每候选至多 1 条；
- valid = 正文明确建立了该变更（有过渡/来源/变更记录）；invalid = 正文直接表现新值但无任何建立交代（无过渡推翻 / 无来源却示人 / 无变更却相反）；
- evidence 必须逐字引用当前章正文（不得改写、不得拼接）；key 必须来自给定候选；
- 证据不足 / 边界情形 → valid（不报）；
- 全部变更均已建立 → 输出空数组 {"judgments": []}。"""


def _join(ctx_items: list[dict], render) -> str:
    return "\n".join(render(i) for i in ctx_items)


def _render_fact(item: dict) -> str:
    """渲染硬约束/事实为可读文本（§7.2 硬约束恒在 Top-K——注入内容而非裸 id）。"""
    content = item.get("content") or ""
    if content:
        src = item.get("source_chapter")
        label = f"（自第 {src} 章）" if src else "（设定配置）"
        return f"- [硬约束/事实] {content} {label}"
    return f"- [硬约束/事实] {item.get('fact_id', '')} (chapter {item.get('source_chapter', '?')})"


def _render_event(item: dict) -> str:
    """渲染事件为可读文本：摘要优先，缺省回退 event_id。"""
    summary = (item.get("summary") or "").strip()
    head = f"{item.get('event_id', '')} " if not summary else ""
    return f"- [事件] {head}{summary} (chapter {item.get('chapter', '?')}, conf {item.get('confidence', '?')})"


def _render_entity(item: dict) -> str:
    state = item.get("state", {})
    relations = item.get("relations", [])
    line = f"- [{item.get('name')}] 境界上限={item.get('realm_cap')} 状态={state}"
    if relations:
        rels = ", ".join(
            f"→{r.get('target')}={r.get('relation_type')}" + (f"(自第{r.get('source_chapter')}章)" if r.get("source_chapter") else "")
            for r in relations
        )
        line += f" 关系: {rels}"
    return line


# 设定实体类型中文名（§7.11 ④）。entity_type 没有 DB CHECK（白名单只在写侧），
# 渲染器必须容忍白名单外的历史值，取不到就原样显示。
_SETTING_TYPE_LABELS = {"item": "物品/武器", "skill": "功法/技能", "location": "地点"}


def _render_setting(item: dict) -> str:
    """渲染设定实体（§7.11 ④）。description 可能显式为 None，不能直接拼接。"""
    etype = _SETTING_TYPE_LABELS.get(item.get("entity_type")) or item.get("entity_type") or "设定"
    seen = item.get("first_seen_chapter")
    line = f"- [{etype}] {item.get('name', '')}"
    if seen:
        line += f"（首见于第 {seen} 章）"
    desc = (item.get("description") or "").strip()
    return f"{line} {desc}" if desc else line


def _render_short(item: dict) -> str:
    return f"- [{item.get('kind')}] {item.get('text') or item.get('summary') or item.get('tail') or ''}"


def _render_foreshadow(item: dict) -> str:
    trigger = item.get("trigger") or {}
    fid = item.get("foreshadow_id")
    id_tag = f" (id: {fid})" if fid else ""
    return (f"- [{item.get('status')}]{id_tag} {item.get('description')} "
            f"(种于第 {item.get('planted_chapter', '?')} 章, 回收条件: "
            f"触发者={trigger.get('actor', '?')} 动作={trigger.get('action', '?')} 对象={trigger.get('object', '?')})")


def _render_thread(item: dict) -> str:
    return (f"- [{item.get('kind')}] {item.get('name')} ({item.get('status')}, "
            f"最近推进第 {item.get('last_progress_chapter') or '?'} 章, 进度: {item.get('progress') or '—'})")


def _render_lesson(item: dict) -> str:
    """渲染一条写作经验（§8.9 reflexion）：内容 + 来源章溯源。"""
    src = item.get("source_chapter")
    label = f"（源自第 {src} 章）" if src else ""
    return f"- [写作经验·{item.get('category')}] {item.get('content')} {label}"


def _lesson_section(context: dict, channels: tuple[str, ...]) -> str:
    """写作经验注入段（§8.9）：按 lesson_type 通道过滤（planning/writing/both）。"""
    items = [i for i in context.get("reflexions", []) if i.get("lesson_type") in channels]
    return "\n".join(_render_lesson(i) for i in items)


def _outline_section(outline: dict | None, *, verbose: bool) -> str:
    """整书大纲注入段：{objective, volume, stage}（按章号切到所属卷/阶段）。"""
    if not outline:
        return ""
    parts: list[str] = []
    if verbose:
        objective = (outline.get("objective") or "").strip()
        if objective:
            parts.append(f"\n【全书 Objective（终局，卷/阶段规划必须逐级逼近）】\n{objective}")
    vol = outline.get("volume") or {}
    if vol:
        vseq = vol.get("volume_seq")
        vtitle = (vol.get("title") or f"第 {vseq} 卷").strip()
        lo, hi = vol.get("chapter_start"), vol.get("chapter_end")
        span = f"第 {lo}–{hi} 章" if lo and hi else ""
        line = f"\n【当前卷 · {vtitle}】"
        if span:
            line += f"{span}；"
        line += f"卷目标：{vol.get('goal') or '—'}"
        theme = (vol.get("theme") or "").strip()
        if theme:
            line += f"；主题：{theme}"
        krs = [str(k).strip() for k in (vol.get("key_results") or []) if str(k).strip()]
        if krs:
            line += "\n本卷关键结果：" + "；".join(krs)
        end_event = (vol.get("end_event") or "").strip()
        if end_event:
            line += f"\n卷末不可逆事件：{end_event}"
        parts.append(line)
    stg = outline.get("stage") or outline.get("current") or {}
    if stg:
        name = (stg.get("name") or f"第 {stg.get('stage_seq') or '?'} 段").strip()
        lo, hi = stg.get("chapter_start"), stg.get("chapter_end")
        span = f"第 {lo}–{hi} 章" if lo and hi else ""
        line = f"\n【当前阶段 · {name}】"
        if span:
            line += f"{span}；"
        line += f"目标：{stg.get('goal') or '—'}"
        beats = [str(b).strip() for b in (stg.get("beats") or []) if str(b).strip()]
        if beats:
            line += "；阶段节拍：" + "；".join(beats)
        if not verbose:
            line += "。本章沿本阶段目标推进，不要提前写本阶段之外的卷末事件；开场勿与其他章共用套路"
        parts.append(line)
    parts.append("\n【大纲是方向参考：与已写正文（前情事件/近期上下文）冲突时，以已写正文为准】")
    return "\n".join(parts)


def _plan_messages(context: dict, batch_goal: str | None = None,
                  outline: dict | None = None) -> list[dict]:
    """plan_chapter 输入：召回上下文 + 批次目标 +（可选）整书大纲切片。

    开放伏笔/剧情线注入（§7.9）：hooks_to_resolve 必须从【开放伏笔】里选——
    防 LLM 编造不存在的伏笔要收，防伏笔烂尾。
    整书大纲注入：outline 是 {objective, volume, stage} 切片，规划沿所属卷/阶段目标推进。
    扫榜灵感已整体前移至建书前（§10：只作建书向导的题材风向工具，不再注入规划节点）。
    """
    facts = _join(context.get("long_term_facts", []), _render_fact)
    events = _join(context.get("mid_term_events", []), _render_event)
    entities = _join(context.get("entity_snapshots", []), _render_entity)
    setting_entities = _join(context.get("setting_snapshots", []), _render_setting)
    short = _join(context.get("short_context", []), _render_short)
    foreshadows = _join(context.get("open_foreshadows", []), _render_foreshadow)
    threads = _join(context.get("plot_threads", []), _render_thread)
    outline_section = _outline_section(outline, verbose=True)

    lessons = _lesson_section(context, ("planning", "both"))
    system = (
        SYSTEM_PLAN
        + "\n\n【世界观硬约束】\n" + (facts or "（无）")
        + "\n【前情事件】\n" + (events or "（无）")
        + "\n【出场人物状态快照】\n" + (entities or "（无）")
        + "\n【设定实体】\n" + (setting_entities or "（无）")
        + outline_section
        + "\n\n【开放伏笔（待回收，hooks_to_resolve 必须从中选，收/延/弃要明确）】\n" + (foreshadows or "（无）")
        + "\n【活跃剧情线（hooks_to_plant 可补新钩子，但主线推进优先）】\n" + (threads or "（无）")
        + "\n【本书写作经验（reflexion 复盘，规划须遵守）】\n" + (lessons or "（无）")
    )
    user_parts = ["【近期上下文】\n" + (short or "（无）"), _opening_section(context)]
    if batch_goal:
        user_parts.append(f"【本批次推进目标】\n{batch_goal}")
    user_parts.append("请输出本章章节计划（严格 JSON）。")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}]


def _profile_list(profile: dict, key: str) -> list[str]:
    """文风档案列表键的安全读取：list→str 清洗；str 非空→单元素；其余→[]。

    防字符串被 join / [*a, *b] 逐字展开（PUT 走 dict 透传，前端可能传 str）。
    """
    val = profile.get(key)
    if isinstance(val, list):
        return [str(v) for v in val if str(v).strip()]
    if isinstance(val, str) and val.strip():
        return [val]
    return []


def _genre_section(genre_pack: dict | None) -> str:
    """本书题材包注入段：节奏/爽点/禁忌/机制；与文风档案分开。"""
    from aiink.genre_catalog import format_prompt
    return format_prompt(genre_pack)


def _style_section(style_profile: dict | None, target_words: int | None) -> str:
    """文风档案注入段（§7.12 / §8.6 生成约束）：字数目标 + 句式/词汇约束 + 对话要求 + 风格示范。

    §7.12 样本提取新增键全部 get() 容错（lexicon_tendency / reference_excerpts /
    frequent_words / 节奏基线），与既有键渲染一致；fatigue_words/forbidden 键不变 →
    L1/L2 检测零回归（样例 15/38/39 锚点）。列表键经 _profile_list 类型守卫。
    """
    parts = []
    if target_words:
        low, high = int(target_words * 0.8), int(target_words * 1.3)
        # 中文 1 字 ≈ 1.4 token：写清换算，避免模型把"3000 字"当"3000 tokens"（实测会超写 40%+）
        parts.append(
            f"目标篇幅：{target_words} 字（约 {int(target_words * 1.4)} tokens）。"
            f"实际输出请控制在 {low}–{high} 字区间，超限会被校验拦截并要求修订。"
        )
    sp = style_profile or {}
    if sp.get("pov"):
        parts.append(f"叙事视角：{sp['pov']}。")
    if sp.get("sentence_style"):
        parts.append(f"句式要求：{sp['sentence_style']}。")
    if sp.get("lexicon_tendency"):
        parts.append(f"词汇修辞倾向：{sp['lexicon_tendency']}。")
    forbidden = _profile_list(sp, "forbidden")
    if forbidden:
        parts.append("表述禁忌（必须避免）：" + "；".join(forbidden) + "。")
    fw = _profile_list(sp, "fatigue_words")
    freq = _profile_list(sp, "frequent_words")
    # §7.12：样本提取产出的高频词串并入写章节制（与显式 fatigue_words 去重合并），不进 L1 阈值
    high_freq = list(dict.fromkeys([*fw, *freq]))
    if high_freq:
        parts.append("高频词节制（避免机械复用）：" + "、".join(high_freq) + "。")
    if sp.get("dialogue"):
        parts.append(f"对话要求：{sp['dialogue']}。")
    rhythm = _rhythm_reference(sp)
    if rhythm:
        parts.append(rhythm)
    excerpts = _profile_list(sp, "reference_excerpts")
    if excerpts:
        parts.append("风格示范（作者样本摘录，模仿其文风、不逐字复制）：\n"
                     + "\n".join(f"- {e}" for e in excerpts))
    return "\n".join(parts)


def _rhythm_reference(sp: dict) -> str:
    """节奏基线一行（§7.12 样本提取）：样本平均句长 + 对话占比 → 写章节奏参考（容错缺键）。"""
    dist = sp.get("sentence_len_dist")
    if not isinstance(dist, dict) or not dist.get("avg"):
        return ""
    line = f"节奏参考：样本平均句长 {dist['avg']} 字。"
    ratio = sp.get("dialogue_ratio")
    if isinstance(ratio, (int, float)) and 0 <= ratio <= 1:
        line += (f" 对话占比约 {ratio * 100:.0f}%，对话偏{'多' if ratio >= 0.4 else '少'}"
                 f"，写章对话密度请贴近样本。")
    return line


def _write_messages(context: dict, plan: dict, *, style_profile: dict | None = None,
                   target_words: int | None = None, outline: dict | None = None,
                   genre_pack: dict | None = None) -> list[dict]:
    """write 输入：召回上下文 + 章节计划 + 文风/字数生成约束（§7.12）+（可选）大纲切片。

    近期章头用于差异化比较，前章章尾用于接续，大纲提供本章目标；三者不可混淆。
    整书大纲（§11）：注入当前卷（目标/关键结果）+ 本章大纲位，写作贴大纲不跑偏；大纲与已写正文冲突信正文。
    """
    facts = _join(context.get("long_term_facts", []), _render_fact)
    entities = _join(context.get("entity_snapshots", []), _render_entity)
    setting_entities = _join(context.get("setting_snapshots", []), _render_setting)
    short = _join(context.get("short_context", []), _render_short)
    style = _style_section(style_profile, target_words)
    genre = _genre_section(genre_pack)
    lessons = _lesson_section(context, ("writing", "both"))
    outline_section = _outline_section(outline, verbose=False)
    system = (
        SYSTEM_WRITE
        + "\n\n【世界观硬约束】\n" + (facts or "（无）")
        + "\n【人物状态快照】\n" + (entities or "（无）")
        + "\n【设定实体】\n" + (setting_entities or "（无）")
        + (f"\n\n【本书题材（project_settings.genre_pack）】\n{genre}" if genre else "")
        + (f"\n\n【文风要求（project_settings.style_profile）】\n{style}" if style else "")
        + outline_section
        + "\n\n【本书写作经验（reflexion 复盘，写作须遵守）】\n" + (lessons or "（无）")
    )
    user = (
        "【章节计划】\n" + json.dumps(plan, ensure_ascii=False, indent=1)
        + "\n\n【近期上下文】\n" + (short or "（无）")
        + "\n\n" + _opening_section(context)
        + "\n请输出本章正文。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _extract_messages(draft: str, chapter_seq: int, context: dict | None = None) -> list[dict]:
    """extract 输入：正文 + （可选）当前台账快照。

    context（recall 的 RetrievedContext）非空时注入实体状态/关系快照——extract 据此校准
    character_state.old_value 与产出 relation_change 候选（正文-台账语义比对 L2 的证据链入口）。
    """
    ledger_block = ""
    foreshadow_block = ""
    if context:
        entities = _join(context.get("entity_snapshots", []), _render_entity)
        if entities:
            ledger_block = f"\n\n【当前台账快照】（供校准 old_value / 产出 relation_change 候选）\n{entities}"
        foreshadows = _join(context.get("open_foreshadows", []), _render_foreshadow)
        if foreshadows:
            foreshadow_block = f"\n\n【开放伏笔】（foreshadow_touch 的 foreshadow_id 必须取自此处）\n{foreshadows}"
    return [
        {"role": "system", "content": SYSTEM_EXTRACT},
        {"role": "user", "content": f"【章节正文】（第 {chapter_seq} 章）\n{draft}{ledger_block}{foreshadow_block}\n\n请抽取记忆候选（严格 JSON）。"},
    ]


def summary_messages(draft: str, chapter_seq: int) -> list[dict]:
    """章节摘要输入（§7 短期记忆，node_summarize）：落库后追加的 LLM 真摘要。"""
    return [
        {"role": "system", "content": SYSTEM_SUMMARIZE},
        {"role": "user", "content": f"【章节正文】（第 {chapter_seq} 章）\n{draft}\n\n请生成章节摘要（严格 JSON）。"},
    ]


def ledger_l2_messages(judgments: list[dict], draft: str, chapter_seq: int) -> list[dict]:
    """正文-台账语义比对 L2 输入：当前章正文 + 待判候选清单（台账当前值 vs 候选新值）。"""
    rows = []
    for j in judgments:
        if j["kind"] == "relation_change":
            rows.append(f"- [{j['key']}] 关系变更 {j['src_name']}→{j['tgt_name']}: 台账={j['ledger']} → 候选新值={j['new_value']}")
        else:
            rows.append(f"- [{j['key']}] 状态变更 {j['entity_name']}.{j['field']}: 台账={j['ledger']} → 候选新值={j['new_value']}")
    body = "\n".join(rows) or "（无）"
    return [
        {"role": "system", "content": SYSTEM_LEDGER_L2},
        {"role": "user", "content": f"【当前章正文】（第 {chapter_seq} 章）\n{draft}\n\n【候选变更清单】\n{body}\n\n请逐项判定（严格 JSON）。"},
    ]


def revise_messages(draft: str, findings: list[dict], chapter_seq: int, *,
                    context: dict | None = None, plan: dict | None = None,
                    style_profile: dict | None = None, target_words: int | None = None,
                    outline: dict | None = None, genre_pack: dict | None = None) -> list[dict]:
    return fit_prompt(context or {}, lambda c: _revise_messages(
        draft, findings, chapter_seq, c, plan or {}, style_profile, target_words, outline,
        genre_pack),
        settings.request_token_budget - 1000)


def _revise_messages(draft, findings, chapter_seq, context, plan, style_profile, target_words,
                     outline, genre_pack):
    finding_lines = "\n".join(
        f"- [{f.get('conflict_key')}] [{f.get('severity')}] {f.get('conflict_type')}: {f.get('evidence')} | 建议: {f.get('suggestion')}"
        for f in findings
    )
    writing = _write_messages(context, plan, style_profile=style_profile,
                              target_words=target_words, outline=outline,
                              genre_pack=genre_pack)
    return [
        {"role": "system", "content": writing[0]["content"] + "\n\n" + SYSTEM_REVISE},
        {"role": "user", "content": writing[1]["content"].removesuffix("\n请输出本章正文。")
         + f"\n\n【第 {chapter_seq} 章待修正文】\n{draft}\n\n【校验发现】\n{finding_lines}"
         + "\n\n修复问题并保持全章因果、人物状态和篇幅；保留无须修改的有效情节。输出修订后全文与 RESPONSES。"},
    ]


def _audit_messages(draft: str, plan: dict, context: dict, chapter_seq: int,
                    genre_pack: dict | None = None) -> list[dict]:
    """audit 输入：正文 + 章节计划 + 召回上下文（审核中枢做语义审核 + 路由决策）。"""
    plan_str = json.dumps(plan, ensure_ascii=False, indent=1) if plan else "（无章节计划）"
    events = _join(context.get("mid_term_events", []), _render_event)
    foreshadows = _join(context.get("open_foreshadows", []), _render_foreshadow)
    threads = _join(context.get("plot_threads", []), _render_thread)
    user = (
        f"【第 {chapter_seq} 章正文】\n{draft}"
        + f"\n\n【章节计划】\n{plan_str}"
        + "\n\n【世界观硬约束】\n" + (_join(context.get("long_term_facts", []), _render_fact) or "（无）")
        + "\n\n【确定性校验结果（逐条核实，不得忽略字数等重大问题）】\n" + json.dumps(context.get("validation_report") or {}, ensure_ascii=False)
        + "\n\n【剧情上下文】\n" + (threads or "（无）")
        + "\n【开放伏笔】\n" + (foreshadows or "（无）")
        + "\n【近期事件】\n" + (events or "（无）")
        + "\n\n【近期上下文】\n" + (_join(context.get("short_context", []), _render_short) or "（无）")
        + "\n\n" + _opening_section(context)
        + "\n\n【人物状态快照】\n" + (_join(context.get("entity_snapshots", []), _render_entity) or "（无）")
        + "\n【设定实体】\n" + (_join(context.get("setting_snapshots", []), _render_setting) or "（无）")
        + _taboo_hint(genre_pack)
        + "\n\n请审核本章并输出路由决策（严格 JSON）。"
    )
    return [{"role": "system", "content": SYSTEM_AUDIT}, {"role": "user", "content": user}]


def _taboo_hint(genre_pack: dict | None) -> str:
    from aiink.genre_catalog import taboo_hints
    items = taboo_hints(genre_pack)
    if not items:
        return ""
    return "\n\n【题材禁忌（提示，不作为硬失败）】\n" + "；".join(items)


def _opening_section(context: dict) -> str:
    rows = _join(context.get("recent_openings", []),
                 lambda row: f"第 {row['chapter']} 章开头：{row['text']}")
    return "【近期章节开头（仅作差异化参照，勿照搬；接续位置以章尾为准）】\n" + (rows or "（无）")


def reflexion_messages(findings: list[dict], existing_lessons: list[dict],
                       start_chapter: int, size: int) -> list[dict]:
    """reflexion 提炼输入（§8.9）：本批 findings + 本书已有经验（供总结演化）。"""
    finding_lines = "\n".join(
        f"- [{f.get('severity')}] {f.get('conflict_type')}（第 {f.get('_chapter', '?')} 章）: "
        f"{f.get('suggestion') or ''} | 证据: {((f.get('evidence') or [{}])[0].get('quote') or '')[:80]}"
        for f in findings
    )
    existing_lines = "\n".join(
        f"- [{l.get('category')}] {l.get('content')}（复发 {l.get('recurrence_count', 0)} 次）"
        for l in existing_lessons
    ) or "（暂无）"
    user = (
        f"本批次第 {start_chapter}–{start_chapter + size - 1} 章，共 {len(findings)} 项发现：\n{finding_lines}"
        + f"\n\n【本书已有写作经验（总结演化时参考，避免重复新增）】\n{existing_lines}"
        + "\n\n请提炼/演化为本书写作经验（严格 JSON）。"
    )
    return [{"role": "system", "content": SYSTEM_REFLEXION}, {"role": "user", "content": user}]


def global_audit_messages(ctx: dict, window: tuple[int, int]) -> list[dict]:
    """全局审计卷推进输入：卷/阶段规划 + 窗口已写摘要。"""
    objective = (ctx.get("objective") or "").strip() or "（未写终局）"
    vol_blocks = []
    for v in ctx.get("volumes") or []:
        krs = "；".join(v.get("key_results") or []) or "—"
        stages = "\n".join(
            f"  - {s.get('name')}（第 {s.get('chapter_start')}–{s.get('chapter_end')} 章）"
            f"目标：{s.get('goal') or '—'}；节拍：{'；'.join(s.get('beats') or []) or '—'}"
            for s in (v.get("stages") or [])
        ) or "  （无阶段）"
        vol_blocks.append(
            f"【第 {v.get('volume_seq')} 卷 · {v.get('title') or ''}】"
            f"第 {v.get('chapter_start')}–{v.get('chapter_end')} 章\n"
            f"卷目标：{v.get('goal') or '—'}\nKR：{krs}\n"
            f"卷末事件：{v.get('end_event') or '—'}\n阶段：\n{stages}"
        )
    progress = "\n".join(
        f"- 第 {p['seq']} 章：{p['text']}" for p in (ctx.get("progress") or [])
    ) or "（窗口内无摘要）"
    user = (
        f"审计窗口：第 {window[0]}–{window[1]} 章。对照卷规划判断是否推进到位。\n\n"
        f"【全书 Objective】\n{objective}\n\n"
        + "\n\n".join(vol_blocks)
        + f"\n\n【窗口已写章节】\n{progress}\n\n请输出严格 JSON（沿目标推进则空数组）。"
    )
    return [{"role": "system", "content": SYSTEM_GLOBAL_AUDIT}, {"role": "user", "content": user}]


SYSTEM_STYLE_EXTRACT = """你是长篇网文创作系统的【文风提炼 Agent】。把作者提交的样本正文提炼成该书可复用的文风档案草稿（§7.12 样本提取）。
输入：① 作者样本（1–2 篇）；② 对样本的确定性统计（句长分布 / 对话密度 / 段落结构 / 高频词串——数字只作参考，语义提炼以样本正文为准）。
输出严格 JSON 对象：
{
  "pov": "叙事人称与视角（如：第三人称限知、以主角为主；样本无稳定倾向写「未从样本提炼」）",
  "sentence_style": "句式与节奏习惯（长短句偏好 / 段落疏密 / 避免机械交替；样本无稳定倾向写「未从样本提炼」）",
  "lexicon_tendency": "词汇与修辞倾向（用词色彩 / 意象 / 比喻习惯）",
  "dialogue": "对话腔调要求（角色区分度 / 口语化程度）",
  "forbidden": ["样本中反复暴露的滥俗 / AI 味表达（2–4 条，具体可执行）"],
  "reference_excerpts": ["1–2 段最能代表该文风的样本原文（逐字摘自样本，供写章作风格示范）"]
}
规则：
- 只提炼样本中真实、反复出现的特征，不臆造；样本信息不足的字段填「未从样本提炼」或省略该键；
- forbidden 只列样本里确实反复出现 / 暴露问题的表达，宁缺毋滥；
- reference_excerpts 必须逐字摘自样本原文（不得改写、不得拼接），1–2 段即可。"""


def style_extract_messages(samples: list[str], stats: dict) -> list[dict]:
    """文风样本提炼输入（§7.12 样本提取）：作者样本正文 + 确定性统计（数字只作提炼参考）。"""
    sample_block = "\n\n".join(f"【样本 {i + 1}】\n{s}" for i, s in enumerate(samples))
    dist = stats.get("sentence_len_dist") or {}
    para = stats.get("para_stats") or {}
    freq = "、".join(stats.get("frequent_words") or []) or "（无）"
    stats_block = (
        f"- 句长分布（字）：短<15 {dist.get('short')} / 中15-40 {dist.get('mid')} / "
        f"长>40 {dist.get('long')}，平均 {dist.get('avg')} 字\n"
        f"- 对话占比：{stats.get('dialogue_ratio')}\n"
        f"- 段落结构：{para.get('count')} 段，平均 {para.get('avg_len')} 字/段\n"
        f"- 高频词串（2 字）：{freq}"
    )
    return [
        {"role": "system", "content": SYSTEM_STYLE_EXTRACT},
        {"role": "user", "content": f"【作者样本】\n{sample_block}\n\n"
                                    f"【确定性统计（仅参考，语义以样本为准）】\n{stats_block}\n\n"
                                    f"请提炼文风档案草稿（严格 JSON）。"},
    ]


SYSTEM_BOOK_SETUP = """你是长篇网文创作系统的【规划 Agent】。职责：根据作者的一句话梗概与题材偏好，产出本书的**设定骨架草稿**（§7.11 建书流程：提案→确认→落库，agent 只提案不篡改）。
输出严格 JSON 对象（schema 见下），字段不许缺：
{
  "title": "书名建议（作者未定书名时给 2-8 字主标题；已定书名返回空串）",
  "realm_order": ["境界/实力阶段按升序排列，非仙侠题材则给出实力/职业进阶序列"],
  "world_rules": {"规则键": "规则值，如 时间/地域/禁制 等世界观硬性规定"},
  "hard_constraints": ["写作必须遵守的硬约束，如 不可越级晋升、不得引入仙佛鬼神"],
  "forces": [{"name": "势力名", "stance": "立场/主张", "resources": ["资源"]}],
  "characters": [{"name": "人物名", "role": "主角/重要配角/反派", "race": "", "origin": "出身", "realm_cap": "实力上限（战力硬约束，非仙侠题材给定位）", "personality": "性格基调一句话"}],
  "locations": [{"name": "关键地点名"}]
}
要求：骨架是**可编辑草稿**不是定稿——数量克制（核心 3-6 个角色、2-4 个势力、3-5 个地点即可），留白让作者后续补全；hard_constraints 必须是明确的、可执行的写作纪律，不是风格形容词；作者未定书名时 title 给出简练的主标题（2-8 字），已定书名则返回空串。"""


def book_setup_messages(genre: str, premise: str, *,
                        genre_pack: dict | None = None) -> list[dict]:
    """建书设定草稿输入（§7.11 ② 一句话梗概启动 + Planner 提案）：题材 + 作者一句话梗概。

    json_mode 调用（prompt 含 "json" 字样）；Planner 复用（不新增 agent），生成的是
    可编辑骨架，不落库——用户逐项确认/修改后走 setup 端点落库。
    """
    pack = _genre_section(genre_pack)
    extra = f"\n\n【本书题材包】\n{pack}" if pack else ""
    return [
        {"role": "system", "content": SYSTEM_BOOK_SETUP},
        {"role": "user", "content": f"【题材偏好】\n{genre}\n\n"
                                    f"【作者一句话梗概】\n{premise}{extra}\n\n"
                                    f"请产出本书设定骨架草稿（严格 JSON）。"},
    ]


def book_outline_messages(genre: str, premise: str, chapter_count: int,
                          storyline: str, *, genre_pack: dict | None = None) -> list[dict]:
    """整书大纲草稿：题材节奏决定卷数，阶段约每 30 章一段，禁止逐章细纲。"""
    from aiink.genre_catalog import suggest_volume_count, volume_span_for
    from aiink.workflow.outline import STAGE_SPAN

    span = volume_span_for(genre_pack)
    nvol = suggest_volume_count(chapter_count, genre_pack)
    parts = [f"【题材】\n{genre}", f"【一句话梗概】\n{premise}",
             f"【大致章节数】\n{chapter_count}",
             f"【分卷约束】按本题材节奏，建议约 {nvol} 卷（每卷约 {span} 章）。"
             f"快节奏卷多卷短，慢节奏卷少卷长。"
             f"每卷超过 {STAGE_SPAN} 章必须再拆成约 {STAGE_SPAN} 章一段的阶段细纲。"
             f"禁止逐章大纲。"]
    pack = _genre_section(genre_pack)
    if pack:
        parts.append(f"【本书题材包】\n{pack}")
    if storyline.strip():
        parts.append(f"【大致故事线】\n{storyline.strip()}")
    return [
        {"role": "system", "content": SYSTEM_BOOK_OUTLINE},
        {"role": "user", "content": "\n\n".join(parts) + "\n\n请产出整书写作大纲（严格 JSON）。"},
    ]


# 工具 schema 单独计入预算，另留 1,000 估算 tokens 给纠错/工具轮消息。
def _tool_prompt_budget() -> int:
    tool_overhead = estimate_tokens({"messages": [], "tools": READ_TOOLS}) - estimate_tokens([])
    return settings.request_token_budget - tool_overhead - 1000


def plan_messages(context: dict, batch_goal: str | None = None,
                  outline: dict | None = None) -> list[dict]:
    return fit_prompt(context, lambda c: _plan_messages(c, batch_goal, outline),
                      settings.request_token_budget - 1000)


def write_messages(context: dict, plan: dict, *, style_profile: dict | None = None,
                   target_words: int | None = None, outline: dict | None = None,
                   genre_pack: dict | None = None) -> list[dict]:
    return fit_prompt(context, lambda c: _write_messages(c, plan, style_profile=style_profile,
                      target_words=target_words, outline=outline, genre_pack=genre_pack),
                      _tool_prompt_budget())


def extract_messages(draft: str, chapter_seq: int, context: dict | None = None) -> list[dict]:
    return fit_prompt(context or {}, lambda c: _extract_messages(draft, chapter_seq, c) + [{"role": "user", "content": "作者评审意见：\n" + _join([r for r in c.get("short_context", []) if r.get("kind") in ("author_review", "user_instruction")], _render_short)}],
                      settings.request_token_budget - 1000)


def audit_messages(draft: str, plan: dict, context: dict, chapter_seq: int,
                   genre_pack: dict | None = None) -> list[dict]:
    return fit_prompt(context, lambda c: _audit_messages(
        draft, plan, c, chapter_seq, genre_pack), _tool_prompt_budget())
