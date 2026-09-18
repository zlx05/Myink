"""L1 高频句式统计（conflict-samples.md 样例 15 代码级落地，§8.6 AI 味治理检测侧）。

确定性、无模型、每章 hint 不阻塞：本章 draft 中 style_profile.fatigue_words（词级）/
fatigue_patterns（句式 regex）频次统计，单句式 ≥2 次 / 单词 ≥3 次 → 1 条 style/hint/local。
宁缺毋滥（§8.8）：低于阈值一律 0 检出（样例 15 单章内 2 处「不是…而是…」恰在边界）；
无 draft / 无 fatigue 字段 / 无 settings / 非法 regex → 静默跳过不抛错（§6.12）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from myink.db import tenant_session
from myink.models import ProjectSettings
from myink.schemas import MutationCandidate
from myink.validation.l1 import L1Validator
from myink.validation.service import ValidationService

REALM_ORDER = ["炼气", "筑基", "金丹", "元婴", "化神", "大乘", "渡劫"]

# 样例 15 冲突片段：连续 3 章结尾「不是…而是…」（单章内 2 处即命中阈值）
SAMPLE15_DRAFT = ("他不是不知道前路凶险，而是早已没有退路。"
                  "他不是畏惧强敌，而是害怕辜负。")

PROFILE = {
    "fatigue_words": ["仿佛", "竟然"],
    "fatigue_patterns": ["不是.{0,20}而是"],
}


def _set_profile(pid: str, profile: dict) -> None:
    """改写该书 project_settings.style_profile（temp_project 克隆 demo，显式覆盖以确定测试输入）。"""
    with tenant_session(pid) as db:
        st = db.execute(select(ProjectSettings).where(
            ProjectSettings.project_id == uuid.UUID(pid))).scalar_one_or_none()
        if st is None:
            db.add(ProjectSettings(project_id=uuid.UUID(pid), world_rules={},
                                   style_profile=profile, hard_constraints=[]))
        else:
            st.style_profile = profile
        db.commit()


def _check(pid: str, draft, seq: int = 15) -> list[dict]:
    with tenant_session(pid) as db:
        findings = L1Validator(REALM_ORDER).style_repeat_check(
            db, project_id=uuid.UUID(pid), chapter_seq=seq, draft=draft)
        return [f.model_dump(mode="json") for f in findings]


# ---- 样例 15（阳性）：句式模式频次超阈值 → style/hint/local ----

def test_sample15_pattern_detected(temp_project):
    """「不是…而是…」单章 2 处 → 1 条 style/hint/local/L1，evidence 带句式与阈值。"""
    _set_profile(temp_project, PROFILE)
    fs = _check(temp_project, SAMPLE15_DRAFT)
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "style"
    assert fs[0]["severity"] == "hint"
    assert fs[0]["scope"] == "local"
    assert fs[0]["source"] == "L1"
    assert "不是.{0,20}而是" in fs[0]["evidence"][0]["quote"], fs[0]["evidence"]


def test_fatigue_word_over_cap(temp_project):
    """单高频词单章 ≥3 次 → 命中。"""
    _set_profile(temp_project, PROFILE)
    fs = _check(temp_project, "仿佛眼前一花，仿佛听见低语，仿佛置身梦境。")
    assert len(fs) == 1, fs
    assert "仿佛" in fs[0]["evidence"][0]["quote"], fs[0]["evidence"]


def test_below_threshold_clean(temp_project):
    """词 2 次 + 句式 1 次 → 均未达阈值，宁缺毋滥 0 检出。"""
    _set_profile(temp_project, PROFILE)
    fs = _check(temp_project, "仿佛夜色沉沉。仿佛有风。他不是畏惧强敌。")
    assert fs == [], fs


def test_no_draft_skips(temp_project):
    """draft 为空 → 跳过。"""
    _set_profile(temp_project, PROFILE)
    assert _check(temp_project, None) == []
    assert _check(temp_project, "") == []


def test_no_fatigue_fields_skips(temp_project):
    """style_profile 无 fatigue 字段 → 跳过。"""
    _set_profile(temp_project, {"pov": "第三人称"})
    assert _check(temp_project, SAMPLE15_DRAFT) == []


def test_no_settings_skips(temp_project):
    """无 ProjectSettings 行 → 静默跳过不抛错。"""
    with tenant_session(temp_project) as db:
        db.execute(ProjectSettings.__table__.delete().where(
            ProjectSettings.project_id == uuid.UUID(temp_project)))
        db.commit()
    assert _check(temp_project, SAMPLE15_DRAFT) == []


def test_multiple_offenders_one_finding(temp_project):
    """词 + 句式同时超阈值 → 至多 1 条（合并 offender，取 top 3）。"""
    _set_profile(temp_project, PROFILE)
    draft = "仿佛仿佛仿佛。" + SAMPLE15_DRAFT  # 词×3 + 句式×2
    fs = _check(temp_project, draft)
    assert len(fs) == 1, fs
    assert "仿佛" in fs[0]["evidence"][0]["quote"], fs[0]["evidence"]
    assert "不是.{0,20}而是" in fs[0]["evidence"][0]["quote"], fs[0]["evidence"]


def test_service_wiring_appends_finding(temp_project):
    """经 ValidationService.validate 编排 → 高频句式 hint 进报告（candidates=[] 零干扰）。"""
    _set_profile(temp_project, PROFILE)
    with tenant_session(temp_project) as db:
        report = ValidationService(REALM_ORDER).validate(
            db, project_id=uuid.UUID(temp_project), chapter_seq=15,
            candidates=[], draft=SAMPLE15_DRAFT)
    style = [f for f in report.findings if f.conflict_type == "style"]
    assert any(f.severity == "hint" and f.scope == "local" and "不是.{0,20}而是" in
               (f.evidence[0].quote if f.evidence else "") for f in style), report.findings
