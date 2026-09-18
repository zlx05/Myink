"""章节接续的文本证据处理；语义重复/衔接判断由已有 audit 节点完成。"""
from __future__ import annotations

import re


def opening_excerpt(content: str | None, cap: int = 220) -> str:
    """跳过导出的标题/分隔线，保留真实开头（不能让重复章标题冒充正文雷同）。"""
    lines = (content or "").strip().splitlines()
    while lines and (not lines[0].strip() or re.fullmatch(
        r"(?:#{1,6}\s+.*|第[\d一二三四五六七八九十百千万零两]+[章节卷].*|[-*_]{3,}|=== CONTENT ===)",
        lines[0].strip(),
    )):
        lines.pop(0)
    return "\n".join(lines).strip()[:cap]


def check_transition_anchor(plan: dict, context: dict) -> None:
    """有接续计划时，证据必须来自当前召回的章尾；兼容无 transition 的旧计划。"""
    transition = plan.get("transition")
    if not transition:
        return
    tails = [row.get("tail", "") for row in context.get("short_context", [])
             if row.get("kind") == "prev_chapter_tail" and row.get("tail")]
    quote = transition.get("anchor_quote", "").strip()
    if tails and (not quote or not any(quote in tail for tail in tails)):
        raise ValueError("transition.anchor_quote 必须逐字引用上一章结尾，不能编造或引用章头")
    if not tails and quote:
        raise ValueError("缺少上一章结尾原文，transition.anchor_quote 必须为空")
    if transition.get("mode") in ("time_jump", "scene_cut") and not transition.get("bridge", "").strip():
        raise ValueError("转场/跳时必须提供 bridge，交代原悬念与新场景的关系")


def repair_generated_transition_anchor(plan: dict, context: dict, cap: int = 120) -> dict:
    """把模型编造或改写的章尾引文替换为上下文中的逐字证据。

    ``anchor_quote`` 不承载剧情决策，只是接续校验的证据。模型少字、换标点或误引章头时，
    重跑整份 Plan 会改变本已合格的场景安排；这里复制计划并只纠正该证据字段。手动编辑
    Plan 仍走严格 ``check_transition_anchor``，不会覆盖作者输入。
    """
    transition = plan.get("transition")
    if not isinstance(transition, dict):
        return plan
    tails = [str(row.get("tail") or "").strip() for row in context.get("short_context", [])
             if row.get("kind") == "prev_chapter_tail" and str(row.get("tail") or "").strip()]
    quote = str(transition.get("anchor_quote") or "").strip()
    if tails and any(quote and quote in tail for tail in tails):
        return plan
    repaired = dict(plan)
    repaired_transition = dict(transition)
    if tails:
        # 取最近章尾最后一段的末尾，限制长度但保持逐字子串；它最接近本章承接动作。
        tail = tails[-1]
        last_line = next((line.strip() for line in reversed(tail.splitlines()) if line.strip()), tail)
        repaired_transition["anchor_quote"] = last_line[-cap:].strip()
    else:
        repaired_transition["anchor_quote"] = ""
    repaired["transition"] = repaired_transition
    return repaired
