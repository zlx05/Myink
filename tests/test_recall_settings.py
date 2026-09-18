"""设定实体进提示词（§7.11 ④，审计报告第 5 项）。

`Entity` 此前只写不读：自动建档的物品/功法/地点永远进不了任何提示词，等于功能死路。
本文件覆盖 `recall._setting_snapshots` 的取样规则：

- first_seen_chapter 门槛——第 5 章首见的武器不能出现在第 3 章的提示词里；
- 本章场景地点名（由 plan_cast 定下后传入）命中的实体优先，其余按创建时间倒序补足；
- 与人物快照同名者排除（由 entity_snapshots 渲染，避免同一名字出现两次）；
- cap 上限。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from aiink.db import tenant_session
from aiink.memory.recall import _MAX_SETTINGS, build_context
from aiink.models import Character, Entity

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _add_entity(db, pid, etype: str, name: str, *, first_seen=None,
                desc=None, created: datetime | None = None) -> Entity:
    ent = Entity(project_id=pid, entity_type=etype, canonical_name=name,
                 properties={"description": desc, "first_seen_chapter": first_seen})
    if created is not None:
        ent.created_at = created
    db.add(ent)
    return ent


def _names(ctx) -> list[str]:
    return [s["name"] for s in ctx.setting_snapshots]


def test_first_seen_gate_excludes_this_chapter_and_later(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        _add_entity(db, pid, "item", "青锋剑", first_seen=3)
        _add_entity(db, pid, "item", "未来刀", first_seen=4)   # 与本章同章 → 排除
        _add_entity(db, pid, "item", "更远之刃", first_seen=9)
        _add_entity(db, pid, "item", "无首见章")
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=4)
    assert "青锋剑" in _names(ctx)
    assert "无首见章" in _names(ctx), "缺 first_seen_chapter 一律放行（JSON 列无 schema）"
    assert "未来刀" not in _names(ctx)
    assert "更远之刃" not in _names(ctx)


def test_scene_location_name_ranks_first(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        _add_entity(db, pid, "item", "旧物", first_seen=1, created=_BASE)
        _add_entity(db, pid, "item", "新物", first_seen=1, created=_BASE + timedelta(days=2))
        _add_entity(db, pid, "location", "黑风寨", first_seen=1, created=_BASE - timedelta(days=5))
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=4, scene_names=["黑风寨"])
    assert _names(ctx) == ["黑风寨", "新物", "旧物"], "场景地点名命中者优先，其余按创建时间倒序"


def test_entity_sharing_character_name_is_excluded(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        db.add(Character(project_id=pid, name="林砚", realm_cap="金丹"))
        _add_entity(db, pid, "item", "林砚")
        _add_entity(db, pid, "item", "青锋剑", first_seen=1)
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=4, participants=["林砚"])
    assert [s["name"] for s in ctx.setting_snapshots] == ["青锋剑"]


def test_description_none_becomes_empty_string_not_none_literal(temp_project):
    """new_entity 显式存 None（无描述时），渲染层不能拼出 "None"。"""
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        _add_entity(db, pid, "skill", "九转炼体诀", first_seen=2, desc=None)
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=4)
    assert ctx.setting_snapshots[0]["description"] == ""


def test_no_entities_yields_empty_list(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        ctx = build_context(db, project_id=pid, chapter_seq=4)
    assert ctx.setting_snapshots == []


def test_capped_at_max_settings(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        for i in range(_MAX_SETTINGS + 4):
            _add_entity(db, pid, "item", f"器物{i}", first_seen=1,
                        created=_BASE + timedelta(minutes=i))
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=9)
    assert len(ctx.setting_snapshots) == _MAX_SETTINGS
