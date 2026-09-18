"""L1 长线债务检查（conflict-samples.md 样例 18/19/23/26/27 代码级落地，§8.6 P1/§7.9）。

确定性、无模型、每章 hint 不阻塞（node_validate 只把 critical/major 当 unresolved）：
- 伏笔烂尾：planted + 空 trigger + 15 章无触碰（样例 18）；developing + 10 章不落地（样例 23，
  以 developing 状态作"已承诺"的确定性代理，不做 trigger 成熟度语义判断——那属 L2/阶段 3 主体）；
- 主线停滞：kind=main + active + 15 章未推进（样例 19）。
阴性必须 0 检出（0 误报）：有回收条件的 planted 长沉（样例 26）、side 支线休眠（样例 27）。
"""

from __future__ import annotations

import uuid

from myink.db import tenant_session
from myink.models import Foreshadow, PlotThread
from myink.schemas import MutationCandidate
from myink.validation.l1 import L1Validator

REALM_ORDER = ["炼气", "筑基", "金丹", "元婴", "化神", "大乘", "渡劫"]


def _validate(pid: str, chapter_seq: int) -> list[dict]:
    """直接跑 L1（fresh 书无角色，power_inflation 不干扰；只测债务检查）。"""
    with tenant_session(pid) as db:
        findings = L1Validator(REALM_ORDER).validate(
            db, project_id=uuid.UUID(pid), chapter_seq=chapter_seq, candidates=[],
        )
        return [f.model_dump(mode="json") for f in findings]


def _seed_foreshadow(pid: str, **kw):
    fields = {"description": "", "status": "planted", "planted_chapter": 1,
              "last_touched": 1, "trigger": {}}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(Foreshadow(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _seed_thread(pid: str, **kw):
    fields = {"name": "线程", "kind": "main", "status": "active", "last_progress_chapter": 1}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(PlotThread(project_id=uuid.UUID(pid), **fields))
        db.flush()


# ---- 样例 18：planted + 空 trigger + 15 章无触碰 → 烂尾 hint ----

def test_sample18_foreshadow_planted_no_trigger_long_untouched(temp_project):
    _seed_foreshadow(temp_project, description="青玉匣中封印之物",
                     status="planted", planted_chapter=3, last_touched=3, trigger={})
    fs = _validate(temp_project, 19)  # 距 last_touched 16 章 > 15
    debt = [f for f in fs if f["conflict_type"] == "foreshadow"]
    assert len(debt) == 1, fs
    assert debt[0]["severity"] == "hint" and debt[0]["scope"] == "local"
    assert "青玉匣" in debt[0]["evidence"][0]["quote"]


def test_sample18_not_yet_untouched_within_threshold(temp_project):
    """阈值内不告警（回归：demo seq 1-3 等既有测试不误报）。"""
    _seed_foreshadow(temp_project, description="短伏笔", planted_chapter=3, last_touched=3)
    fs = _validate(temp_project, 12)  # 距 last_touched 9 章 < 15
    assert not [f for f in fs if f["conflict_type"] == "foreshadow"], fs


# ---- 样例 26 阴性：planted + 有回收条件（trigger）→ 刻意长沉，0 误报 ----

def test_sample26_foreshadow_with_trigger_long_sleep_ok(temp_project):
    _seed_foreshadow(temp_project, description="青玉匣封印之物",
                     planted_chapter=3, last_touched=3,
                     trigger={"actor": "林砚", "action": "突破元婴", "object": "青玉匣"})
    fs = _validate(temp_project, 40)  # 即便跨 37 章也不告警
    assert not [f for f in fs if f["conflict_type"] == "foreshadow"], fs


# ---- 样例 23：developing 捡起后 10 章不落地 → 烂尾 hint（不依赖 trigger 成熟度）----

def test_sample23_foreshadow_developing_stalled(temp_project):
    _seed_foreshadow(temp_project, description="玉佩与万魔渊的关联", status="developing",
                     planted_chapter=5, last_touched=12,
                     trigger={"actor": "林砚", "action": "获得玉佩", "object": "玉佩"})
    fs = _validate(temp_project, 23)  # 距 last_touched 11 章 > 10，即便有 trigger 也告警
    debt = [f for f in fs if f["conflict_type"] == "foreshadow"]
    assert len(debt) == 1, fs
    assert debt[0]["severity"] == "hint"


def test_sample23_developing_within_threshold_ok(temp_project):
    _seed_foreshadow(temp_project, description="developing 未停滞", status="developing",
                     planted_chapter=5, last_touched=12)
    fs = _validate(temp_project, 18)  # 距 last_touched 6 章 < 10
    assert not [f for f in fs if f["conflict_type"] == "foreshadow"], fs


# ---- 样例 19：main + active + 15 章未推进 → 停滞 hint ----

def test_sample19_main_thread_stalled(temp_project):
    _seed_thread(temp_project, name="万魔渊线", kind="main", last_progress_chapter=5)
    fs = _validate(temp_project, 25)  # 距 last_progress 20 章 > 15
    debt = [f for f in fs if f["conflict_type"] == "plotline"]
    assert len(debt) == 1, fs
    assert debt[0]["severity"] == "hint" and "万魔渊线" in debt[0]["evidence"][0]["quote"]


def test_sample19_main_thread_within_threshold_ok(temp_project):
    _seed_thread(temp_project, name="近期推进", kind="main", last_progress_chapter=10)
    fs = _validate(temp_project, 18)  # 距 last_progress 8 章 < 15
    assert not [f for f in fs if f["conflict_type"] == "plotline"], fs


# ---- 样例 27 阴性：side 支线长期休眠 → 0 误报 ----

def test_sample27_side_thread_dormant_ok(temp_project):
    _seed_thread(temp_project, name="灵兽谷支线", kind="side", last_progress_chapter=3)
    fs = _validate(temp_project, 26)  # 跨 23 章也不告警
    assert not [f for f in fs if f["conflict_type"] == "plotline"], fs


# ---- 状态边界：closed/resolved/dropped/未设进度锚点 不告警 ----

def test_closed_or_resolved_not_flagged(temp_project):
    _seed_foreshadow(temp_project, description="已收伏笔", status="resolved",
                     planted_chapter=1, last_touched=1)
    _seed_thread(temp_project, name="已关线", kind="main", status="closed",
                 last_progress_chapter=1)
    fs = _validate(temp_project, 50)
    assert not [f for f in fs if f["conflict_type"] in ("foreshadow", "plotline")], fs


def test_no_progress_anchor_not_flagged(temp_project):
    """线程无 last_progress 锚点（None）不告警——宁缺毋滥。

    伏笔相反：planted_chapter 是 NOT NULL 恒锚点，"种下后从未触碰且无 trigger"正是最强烂尾信号
    （样例 18 同形态，last_touched=None 回退 planted_chapter）。
    """
    _seed_thread(temp_project, name="无进度锚点", kind="main", last_progress_chapter=None)
    fs = _validate(temp_project, 40)
    assert not [f for f in fs if f["conflict_type"] == "plotline"], fs

    _seed_foreshadow(temp_project, description="种下后从未触碰", status="planted",
                     planted_chapter=3, last_touched=None, trigger={})
    fs = _validate(temp_project, 40)
    debt = [f for f in fs if f["conflict_type"] == "foreshadow"]
    assert len(debt) == 1, fs


def test_resolved_foreshadow_not_in_open_pool(temp_project):
    """get_open_foreshadows 只取 planted/developing——resolved 天然排除（样例 7 收伏笔链路）。"""
    _seed_foreshadow(temp_project, description="已回收", status="resolved",
                     planted_chapter=1, last_touched=1)
    fs = _validate(temp_project, 30)
    assert not [f for f in fs if f["conflict_type"] == "foreshadow"], fs
