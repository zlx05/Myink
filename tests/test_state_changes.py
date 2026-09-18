"""人物状态快照带「变化」（审计报告第 4 项的提示词侧）。

只给模型当前值（`境界：筑基三层`）时，模型看不出「这一章刚突破」。`get_character_state_detail`
在同样口径下多取一列上一值供 recall 渲染「变化」子句，同时**不改** `get_character_state`
（它有 8 处调用方，改返回类型会大面积回响）。
"""

from __future__ import annotations

import uuid

from myink.db import tenant_session
from myink.memory import repository as repo
from myink.memory.recall import build_context
from myink.models import Character, CharacterState
from myink.workflow.prompts import _render_entity


def _seed_state(pid: str, cid: uuid.UUID, seq: int, *, field: str = "realm",
                new_value: str = "筑基三层", old_value: str | None = None,
                valid_from: int | None = None, valid_to: int | None = None) -> None:
    with tenant_session(pid) as db:
        db.add(CharacterState(
            project_id=uuid.UUID(pid), character_id=cid, chapter_seq=seq, field=field,
            old_value=old_value, new_value=new_value, source_chapter=seq, confidence=0.9,
            valid_from=valid_from if valid_from is not None else seq, valid_to=valid_to))
        db.commit()


# ---- get_character_state_detail ----

def test_detail_falls_back_to_previous_row_new_value(temp_project):
    """台账 old_value 列常为空串（extract 只在能校准时才填）→ 用上一行的 new_value 兜底。"""
    cid = uuid.uuid4()
    _seed_state(temp_project, cid, 5, new_value="练气九层")
    _seed_state(temp_project, cid, 7, new_value="筑基三层", old_value="")
    with tenant_session(temp_project) as db:
        d = repo.get_character_state_detail(db, uuid.UUID(temp_project), cid, chapter_seq=8)
    assert d["realm"]["new_value"] == "筑基三层"
    assert d["realm"]["old_value"] == "练气九层"
    assert d["realm"]["source_chapter"] == 7


def test_detail_prefers_row_old_value(temp_project):
    """行自带的 old_value 非空时以它为准（extract 已按注入快照校准过）。"""
    cid = uuid.uuid4()
    _seed_state(temp_project, cid, 5, new_value="练气九层")
    _seed_state(temp_project, cid, 7, new_value="筑基三层", old_value="练气八层")
    with tenant_session(temp_project) as db:
        d = repo.get_character_state_detail(db, uuid.UUID(temp_project), cid, chapter_seq=8)
    assert d["realm"]["old_value"] == "练气八层"


def test_detail_has_no_old_value_for_single_row_field(temp_project):
    cid = uuid.uuid4()
    _seed_state(temp_project, cid, 7, new_value="筑基三层")
    with tenant_session(temp_project) as db:
        d = repo.get_character_state_detail(db, uuid.UUID(temp_project), cid, chapter_seq=8)
    assert d["realm"]["old_value"] is None


def test_detail_respects_time_window_and_invalidation(temp_project):
    """时间窗与 valid_to 口径与 get_character_state 一致：失效行既不算当前值也不算上一值。"""
    cid = uuid.uuid4()
    _seed_state(temp_project, cid, 5, new_value="旧稿值", valid_to=5)          # 已失效
    _seed_state(temp_project, cid, 6, new_value="未来值", valid_from=10)       # 窗口外
    _seed_state(temp_project, cid, 7, new_value="筑基三层")
    with tenant_session(temp_project) as db:
        d = repo.get_character_state_detail(db, uuid.UUID(temp_project), cid, chapter_seq=8)
    assert d["realm"]["new_value"] == "筑基三层"
    assert d["realm"]["old_value"] is None, "失效行与窗口外行都不能当上一值"


def test_get_character_state_unchanged_by_detail(temp_project):
    """防回响：同一批数据下 detail 的当前值推导必须与 get_character_state 逐字一致。"""
    cid = uuid.uuid4()
    _seed_state(temp_project, cid, 5, field="realm", new_value="练气九层")
    _seed_state(temp_project, cid, 7, field="realm", new_value="筑基三层")
    _seed_state(temp_project, cid, 7, field="injury", new_value="左肩剑伤")
    with tenant_session(temp_project) as db:
        flat = repo.get_character_state(db, uuid.UUID(temp_project), cid, chapter_seq=8)
        detail = repo.get_character_state_detail(db, uuid.UUID(temp_project), cid, chapter_seq=8)
    assert flat == {f: d["new_value"] for f, d in detail.items()}


# ---- recall 快照 ----

def test_recall_snapshot_carries_only_real_transitions(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        ch = Character(project_id=pid, name="林砚", realm_cap="金丹")
        db.add(ch)
        db.flush()
        cid = ch.id
    _seed_state(temp_project, cid, 5, field="realm", new_value="练气九层")
    _seed_state(temp_project, cid, 7, field="realm", new_value="筑基三层")
    _seed_state(temp_project, cid, 7, field="location", new_value="黑风寨")  # 仅一行 → 无变化
    with tenant_session(temp_project) as db:
        ctx = build_context(db, project_id=pid, chapter_seq=8, participants=["林砚"])
    snap = ctx.entity_snapshots[0]
    assert snap["state"] == {"realm": "筑基三层", "location": "黑风寨"}
    assert snap["state_changes"] == {"realm": {"old": "练气九层", "chapter": 7}}


def test_recall_drops_unchanged_field_from_state_changes(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        ch = Character(project_id=pid, name="林砚", realm_cap="金丹")
        db.add(ch)
        db.flush()
        cid = ch.id
    _seed_state(temp_project, cid, 5, field="realm", new_value="筑基三层")
    _seed_state(temp_project, cid, 7, field="realm", new_value="筑基三层")  # 重复记录同一值
    with tenant_session(temp_project) as db:
        ctx = build_context(db, project_id=pid, chapter_seq=8, participants=["林砚"])
    assert ctx.entity_snapshots[0]["state_changes"] == {}


# ---- 渲染 ----

def test_render_entity_appends_change_clause():
    line = _render_entity({
        "name": "林砚", "realm_cap": "金丹",
        "state": {"realm": "筑基三层"},
        "state_changes": {"realm": {"old": "练气九层", "chapter": 7}},
    })
    assert line == ("- [林砚] 境界上限=金丹 状态={'realm': '筑基三层'} "
                    "变化: realm 练气九层→筑基三层(第7章)")


def test_render_entity_without_state_changes_key_is_unchanged():
    """兼容旧字典：test_continuity/test_context_budget 直接构造无 state_changes 的快照。"""
    line = _render_entity({"name": "周野", "state": {"injury": "左腿受伤"}})
    assert line == "- [周野] 境界上限=None 状态={'injury': '左腿受伤'}"
    assert "变化" not in line
