"""校验服务（validate 节点）：L1 确定性 + 大纲偏差比对 + 汇总。

- L1：validation.l1.L1Validator（无模型，纯规则）；
- 大纲偏差：ChapterPlan.expected_events vs extract 实际事件（§8.6，近免费——
  复用 extract 产物，不新增 LLM 调用）→ 偏差报告 → 作者决策（改正文/改大纲）；
- 桥段重复向量近邻（L1 事件层，样例 14/32）——draft 依赖，故由 service 编排而非并入 l1.validate；
- 高频句式统计（L1，样例 15）——draft 依赖，service 编排；
- L2（语义校验）阶段 1 留接口，阶段 3 接 Validator-L2。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from myink.memory import repository as repo
from myink.schemas import ChapterPlan, Finding, MutationCandidate, ValidationReport
from myink.validation.l1 import L1Validator

_OVERLAP_RATIO = 0.4

# 字数门禁（§6.9）：目标字数的可接受区间 [下限, 上限]，超出即 major → 触发修订
_LEN_LOW_RATIO = 0.8
_LEN_HIGH_RATIO = 1.3


def _overlap(a: str, b: str) -> float:
    """字符集重叠率（中文无分词时的近似语义匹配）。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa)


def _entity_names(session: Session, project_id: uuid.UUID) -> list[str]:
    return [c.name for c in repo.get_all_characters(session, project_id)]


def chapter_length_check(chapter_seq: int, draft: str | None,
                         target_words: int | None) -> list[Finding]:
    """字数门禁（§6.9）：正文不在 [0.8×target, 1.3×target] → major → 触发修订。

    超长比过短更伤（水漫金山），但都拦。返回 [] 表示达标。
    """
    if not target_words or not draft:
        return []
    n = len(draft)
    low, high = int(target_words * _LEN_LOW_RATIO), int(target_words * _LEN_HIGH_RATIO)
    if low <= n <= high:
        return []
    over = "过长" if n > high else "过短"
    return [Finding(
        conflict_key=f"len:{chapter_seq}",
        conflict_type="style", severity="major", scope="local", source="L1",
        evidence=[{"chapter": chapter_seq, "quote": f"正文 {n} 字，目标 {target_words} 字（区间 {low}–{high}）"}],
        suggestion=f"本章字数{over}：目标 {target_words} 字，请压缩/扩写到 {low}–{high} 字区间",
    )]


def outline_deviation(session: Session, project_id: uuid.UUID, chapter_seq: int,
                      plan: ChapterPlan | None,
                      candidates: list[MutationCandidate]) -> list[Finding]:
    """expected_events vs extract 实际事件 → 缺失项报告（样例 13）。"""
    if not plan or not plan.expected_events:
        return []

    actual_summaries = [
        c.payload.get("summary", "")
        for c in candidates
        if c.kind == "event" and isinstance(c.payload, dict)
    ]
    names = _entity_names(session, project_id)
    findings: list[Finding] = []
    for expected in plan.expected_events:
        hit = False
        # 关键词命中：expected 中的实体名在任一实际事件摘要出现
        for name in names:
            if name in expected and any(name in s for s in actual_summaries):
                hit = True
                break
        if not hit and any(_overlap(expected, s) >= _OVERLAP_RATIO for s in actual_summaries):
            hit = True
        if not hit:
            findings.append(Finding(
                conflict_key=f"dev:{chapter_seq}:{expected[:16]}",
                conflict_type="plotline", severity="major", scope="structural", source="L1",
                evidence=[{"chapter": chapter_seq, "quote": f"预期事件「{expected}」在正文中无对应"}],
                suggestion="作者决策：改正文拉回 / 改大纲顺水推舟（§8.6）",
            ))
    return findings


class ValidationService:
    """确定性校验编排（只产 L1，零 LLM）。

    正文-台账语义比对 L2（validator_l2 LLM，§8.6 点级）不在本类——LLM 不能进校验器：
    validate 被测试直接调用（test_bridge_repeat/test_style_repeat 无 LLM monkeypatch），
    L2 在 node_validate 接线（nodes.py），validate 只产出可完全确定性断言的 L1 检查。
    """

    def __init__(self, realm_order: list[str]):
        self.l1 = L1Validator(realm_order)

    def validate(self, session: Session, *, project_id: uuid.UUID, chapter_seq: int,
                 candidates: list[MutationCandidate],
                 plan: ChapterPlan | None = None,
                 draft: str | None = None, target_words: int | None = None) -> ValidationReport:
        findings = self.l1.validate(session, project_id=project_id, chapter_seq=chapter_seq, candidates=candidates)
        # 桥段重复向量近邻（§8.6 样例 14/32）：draft 依赖，service 编排；hint 不阻塞
        findings += self.l1.bridge_repeat_check(session, project_id=project_id, chapter_seq=chapter_seq,
                                                candidates=candidates, draft=draft)
        findings += self.l1.style_repeat_check(session, project_id=project_id, chapter_seq=chapter_seq,
                                               draft=draft)
        # 阵营敌对（§8.4 L1 第 1 类，样例 2/40）：draft 依赖，service 编排；硬约束 critical 不阻塞生成
        findings += self.l1.faction_check(session, project_id=project_id, chapter_seq=chapter_seq, draft=draft)
        findings += outline_deviation(session, project_id, chapter_seq, plan, candidates)
        findings += chapter_length_check(chapter_seq, draft, target_words)

        report = ValidationReport(
            project_id=project_id, chapter_seq=chapter_seq, findings=findings,
            summary={
                "total": len(findings),
                "critical": sum(1 for f in findings if f.severity == "critical"),
                "resolved": 0,
            },
        )
        return report
