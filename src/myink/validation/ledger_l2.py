"""正文-台账语义比对 L2（阶段 3 长线治理 · 切片 4 点级，plan.md §8.6 项 7/8）。

对 extract 候选做「正文表现 vs 台账当前值」的语义比对，抓三类矛盾（样例 16/17/20/21/22）：
  - 无过渡推翻：injury/location/goal 等被正文直接翻转为新值，无中间演化（样例 16/20）；
  - 无来源却示人：identity 被直接以新身份示人，无受封/夺权来源（样例 21）；
  - 无变更却相反：relation 表现与台账相反，无变更记录（样例 17/22）。
阴性对照：正文先建立变更（治疗闭关、把酒言和结盟）→ valid 不报（样例 24/25）。

机制：确定性预滤（只判「候选 old_value == 台账当前值 且 new_value ≠ 台账当前值」的候选）
→ 1 次 LLM（validator_l2，判 valid | invalid）→ 本地守卫（evidence 须为内存 draft 逐字子串、
key 在判定集、verdict=invalid、confidence ≥ 阈值）。判定集空 → 零 LLM 调用零成本短路。

与 L1 互补：L1 `_old_value_ledger_check` / `_relation_change_ledger_check` 抓「候选 old_value ≠
台账当前值」（抽取误读快照）；本模块只接「old_value 与台账一致但 new_value 擅自变更」的语义层。
realm/alive 有专属 L1 检查，不进判定集（防同候选双报）。

守卫读内存 draft（validate 跑在 persist 之前，DB Chapter.content 尚不存在），故不复用
global_audit._verify_findings（其读 DB）。

边界（不宣称覆盖）：extract 漏产候选即漏检——证据链必须从候选进入（与 L1 窄脚印同哲学），
不做「无候选也扫正文」的全文比对（成本不可控）。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from myink.memory import repository as repo
from myink.models import Character
from myink.providers import make_chain
from myink.schemas import Finding
from myink.validation.l1 import _GENERAL_STATE_FIELDS, _key

logger = logging.getLogger(__name__)

# 单章判定集上限（防 draft 候选过多撑爆一次 LLM 调用的输出/成本；超限丢弃尾部并记 log）
MAX_LEDGER_L2_CANDIDATES = 8
# 守卫置信度阈值（镜像全局审计 MIN_CONFIDENCE=0.6，宁缺毋滥）
MIN_CONFIDENCE = 0.6
_MAX_LEDGER_L2_TOKENS = 2048

# 确定性严重度/作用域映射（LLM 不参与评级，防抖动；与 spec 样例 16/20/21 预期 Finding 对齐）
_LEDGER_L2_SEVERITY = {
    "identity": ("major", "local"),   # 样例 21：无身份来源却以新身份示人
    "injury": ("major", "local"),     # 样例 16：显式伤情被无过渡推翻
    "location": ("minor", "local"),
    "goal": ("minor", "local"),       # 样例 20：目标无过渡推翻
    "power": ("minor", "local"),
    "item": ("minor", "local"),
    "knowledge": ("minor", "local"),
}
# 关系变更固定 major/structural（样例 17/22：关系自洽跨章属性，非单点）
_RELATION_SEVERITY: tuple[str, str] = ("major", "structural")


def build_judgment_set(session: Session, *, project_id, chapter_seq: int,
                       candidates: list[dict]) -> list[dict]:
    """确定性预滤：挑出「old_value == 台账当前值 且 new_value ≠ 台账当前值」的候选进判定集。

    每项渲染供 LLM 判定的字段（key/类型/实体名/台账当前值/候选新值）。按 conflict_key 去重、
    cap 截断。判定集空 → run_ledger_l2 零 LLM 短路。
    """
    judgments: list[dict] = []
    seen: set[str] = set()

    for cand in candidates:
        payload = cand.get("payload") or {}
        old_v = (payload.get("old_value") or "").strip()
        new_v = (payload.get("new_value") or "").strip()
        if not old_v or not new_v or old_v == new_v:
            continue  # 缺旧/新值或未变更：无从比对
        if cand.get("kind") == "character_state":
            field = payload.get("field")
            ch_id = str(payload.get("character_id", ""))
            if field not in _GENERAL_STATE_FIELDS or not ch_id:
                continue  # realm/alive 有专属 L1 检查；无实体不比对
            ledger = repo.get_character_state(session, project_id, uuid.UUID(ch_id), chapter_seq - 1)
            current = (ledger.get(field) or "").strip()
            if not current or current != old_v:
                continue  # 台账无当前值（首写）或 old≠台账 → L1 域
            key = _key("character_state", f"{ch_id}:{field}", chapter_seq)
            if key in seen:
                continue
            ch = session.get(Character, uuid.UUID(ch_id))
            seen.add(key)
            judgments.append({
                "key": key, "kind": "character_state", "entity": ch_id,
                "entity_name": ch.name if ch else ch_id, "field": field,
                "old_value": old_v, "new_value": new_v, "ledger": current,
            })
        elif cand.get("kind") == "relation_change":
            src = str(payload.get("source_id", ""))
            tgt = str(payload.get("target_id", ""))
            if not src or not tgt:
                continue
            current = repo.get_relation_current_type(session, project_id,
                                                     uuid.UUID(src), uuid.UUID(tgt))
            if not current or current != old_v:
                continue  # 台账无活跃关系 / 歧义(None) / old≠台账 → 跳过（歧义交 L1 兜底）
            key = _key("relation", f"{src}:{tgt}", chapter_seq)
            if key in seen:
                continue
            src_ch = session.get(Character, uuid.UUID(src))
            tgt_ch = session.get(Character, uuid.UUID(tgt))
            seen.add(key)
            judgments.append({
                "key": key, "kind": "relation_change", "src": src, "tgt": tgt,
                "src_name": src_ch.name if src_ch else src,
                "tgt_name": tgt_ch.name if tgt_ch else tgt,
                "relation_type": payload.get("relation_type", ""),
                "old_value": old_v, "new_value": new_v, "ledger": current,
            })
        if len(judgments) >= MAX_LEDGER_L2_CANDIDATES:
            logger.warning("ledger_l2 判定集达 cap=%d，丢弃其余候选（ch%d）",
                           MAX_LEDGER_L2_CANDIDATES, chapter_seq)
            break
    return judgments


def _guard_judgments(judgments: list[dict], raw_items, draft: str,
                     chapter_seq: int) -> list[dict]:
    """本地守卫：LLM judgment 只当 key ∈ 判定集、verdict=invalid、置信度足、
    evidence 为 draft 逐字子串才出 finding（0 误报兜底，不信任 LLM 过度标记）。"""
    judgment_by_key = {j["key"]: j for j in judgments}
    findings: list[dict] = []
    seen: set[str] = set()
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        j = judgment_by_key.get(key)
        if not j or key in seen:
            continue  # 伪造/未知 key 丢弃；每候选至多 1 条
        if item.get("verdict") != "invalid":
            continue
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if confidence < MIN_CONFIDENCE:
            continue
        evidence = (item.get("evidence") or "").strip()
        if not evidence or evidence not in draft:
            continue  # 非逐字子串（改写/拼接/编造）→ 丢
        seen.add(key)
        if j["kind"] == "relation_change":
            severity, scope = _RELATION_SEVERITY
            conflict_type = "relation"
            suggestion = (f"正文表现关系 {j['ledger']}→{j['new_value']} 但无变更记录"
                          f"（无过渡/来源/变更交代）；建议补和解/决裂情节落 relation_change，"
                          f"或改回与台账一致的表现（§8.6）")
        else:
            conflict_type = "character_state"
            severity, scope = _LEDGER_L2_SEVERITY.get(j["field"], ("minor", "local"))
            suggestion = (f"正文对 {j['field']} 变更无过渡/来源交代：台账 {j['field']}={j['ledger']} "
                          f"被直接推翻为 {j['new_value']}；建议补中间演化"
                          f"（治疗/移动/身份来源事件）或撤销该表述（§8.6）")
        findings.append(Finding(
            conflict_key=key, conflict_type=conflict_type, severity=severity,
            scope=scope, source="L2", confidence=confidence,
            evidence=[{"chapter": chapter_seq, "quote": evidence}],
            suggestion=suggestion,
        ).model_dump(mode="json"))
    return findings


def run_ledger_l2(session: Session, *, project_id, chapter_seq: int,
                  candidates: list[dict], draft: str, task_id: str | None = None) -> list[dict]:
    """正文-台账语义比对 L2 编排：预滤 → 1 次 LLM → 本地守卫 → 返回 finding dicts。

    判定集空 → 返回 []（不调 LLM、不记 agent_runs，零成本短路）。LLM/解析失败 → 记 error
    agent_runs 行并返回 []（非阻断，同全局审计语义——L1 窄脚印仍在）。
    """
    from myink.workflow import nodes, prompts  # 懒导入防循环（validation 被 workflow.nodes 引用）

    judgments = build_judgment_set(session, project_id=project_id, chapter_seq=chapter_seq,
                                   candidates=candidates)
    if not judgments:
        return []
    messages = prompts.ledger_l2_messages(judgments, draft, chapter_seq)
    resp = make_chain("validator_l2", db=session, project_id=str(project_id)).generate(messages, json_mode=True,
                                                                                       max_tokens=_MAX_LEDGER_L2_TOKENS)
    error = resp.error
    findings: list[dict] = []
    if not error:
        try:
            data = nodes._parse_json(resp.content)
            raw_items = data.get("judgments", []) if isinstance(data, dict) else []
            findings = _guard_judgments(judgments, raw_items, draft, chapter_seq)
        except Exception as exc:  # noqa: BLE001 —— 解析失败同 LLM 失败（§6.12）
            error = f"ledger_l2 解析失败: {exc}"
    nodes.record_run(session, project_id=str(project_id), task_id=task_id, node="validate",
                     role="LedgerL2", resp=resp, error=error, messages=messages,
                     detail={"l2_judgments": len(judgments), "l2_findings": len(findings)})
    return findings
