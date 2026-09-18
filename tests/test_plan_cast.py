"""规划的先后顺序（§3 鸡生蛋）：先定出场人物与场景地点，再据此取台账。

`recall` 跑在 `plan_chapter` 之前，而「取谁的状态」本该由规划决定。旧实现只能拿
`get_all_characters` 按姓名排序的前 12 人充数，与本章出场无关。`plan_cast` 把规划拆成
两拍：第一拍只定 cast/locations，据此重取一次 `build_context`；第二拍才产出完整计划。

本文件覆盖这条链路的四个行为：

- 显式传入的 scene_names 决定设定实体排序（首次生成时本章计划尚未落库，正是这条路径）；
- cast 定下的人决定 entity_snapshots，而不是名单前 12 人；
- 空 cast 被 `ChapterCast` 拒收，不留进状态；
- replan 反馈跨过这次重取存活（重取会重建 context，不显式带回就丢）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from aiink.db import tenant_session
from aiink.memory.recall import build_context
from aiink.models import Character, Entity
from aiink.providers.base import ModelProvider, ModelResponse
from aiink.workflow import nodes

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _CastProvider(ModelProvider):
    """固定返回一份 cast 的假 provider（plan_cast 是本文件唯一被测节点）。"""

    def __init__(self, cast: list[str], locations: list[str] | None = None):
        self.cast = cast
        self.locations = locations or []
        self.calls: list[str] = []

    def name(self) -> str:
        return "cast-stub"

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None,
                 json_mode=False, tools=None, disable_thinking=False):
        self.calls.append(messages[0]["content"])
        content = json.dumps({"cast": self.cast, "locations": self.locations},
                             ensure_ascii=False)
        return ModelResponse(content=content, model_id=model_id, input_tokens=1,
                             output_tokens=1, duration_ms=1)


def _add_entity(db, pid, etype: str, name: str, *, first_seen=None,
                created: datetime | None = None) -> Entity:
    ent = Entity(project_id=pid, entity_type=etype, canonical_name=name,
                 properties={"description": "", "first_seen_chapter": first_seen})
    if created is not None:
        ent.created_at = created
    db.add(ent)
    return ent


def _names(ctx) -> list[str]:
    return [s["name"] for s in ctx.setting_snapshots]


def test_explicit_scene_names_rank_without_any_chapter_outline(temp_project):
    """plan_cast 直接给地点名——首次生成本章时计划表里查不到任何行，不能指望回退读计划。"""
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        _add_entity(db, pid, "item", "旧物", first_seen=1, created=_BASE)
        _add_entity(db, pid, "item", "新物", first_seen=1, created=_BASE + timedelta(days=2))
        _add_entity(db, pid, "location", "黑风寨", first_seen=1, created=_BASE - timedelta(days=5))
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=4, scene_names=["黑风寨"])
    assert _names(ctx) == ["黑风寨", "新物", "旧物"], "地点名命中者优先，其余按创建时间倒序"


def test_plan_cast_snapshots_follow_the_cast_not_the_roster(temp_project, monkeypatch):
    """快照取自 cast。名单前 12 人全是路人时，若还按旧口径取人，林砚/苏晚会整个缺席。"""
    import aiink.providers as providers_mod

    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        for i in range(15):
            db.add(Character(project_id=pid, name=f"路人{i:02d}", realm_cap="练气"))
        for name in ("林砚", "苏晚"):
            db.add(Character(project_id=pid, name=name, realm_cap="金丹"))
        db.flush()

    roster = [f"路人{i:02d}" for i in range(15)] + ["林砚", "苏晚"]
    provider = _CastProvider(cast=["林砚", "苏晚"], locations=["山门"])
    monkeypatch.setattr(providers_mod, "default_provider", provider)

    out = nodes.node_plan_cast({
        "project_id": temp_project, "chapter_seq": 1,
        "characters": [{"name": n} for n in roster], "context": {},
    })

    assert out["cast"] == {"cast": ["林砚", "苏晚"], "locations": ["山门"]}
    names = [s["name"] for s in out["context"]["entity_snapshots"]]
    assert names == ["林砚", "苏晚"]
    assert "路人00" not in names
    # 名单（只给名字，不给状态）确实进了提示词——cast 要落在真实存在的角色上
    assert "路人00" in provider.calls[0]


def test_plan_cast_rejects_empty_cast_and_keeps_state_untouched(temp_project, monkeypatch):
    """空 cast 无意义：`ChapterCast` 的 min_length=1 兜住，error 透传而不写入半成品。"""
    import aiink.providers as providers_mod

    provider = _CastProvider(cast=[])
    monkeypatch.setattr(providers_mod, "default_provider", provider)

    out = nodes.node_plan_cast({
        "project_id": temp_project, "chapter_seq": 1, "characters": [], "context": {},
    })

    assert "error" in out
    assert "plan_cast 输出多次不合格" in out["error"]
    assert "cast" not in out and "context" not in out


def test_plan_cast_carries_replan_feedback_across_the_context_rebuild(temp_project, monkeypatch):
    """重取上下文会重建 short_context；replan 反馈必须显式带回，否则重规划看不到失败原因。"""
    import aiink.providers as providers_mod

    provider = _CastProvider(cast=["林砚"])
    monkeypatch.setattr(providers_mod, "default_provider", provider)
    feedback = {"kind": "replan_feedback", "reasons": ["上一版把主角写死了"], "chapter": 1}

    out = nodes.node_plan_cast({
        "project_id": temp_project, "chapter_seq": 1, "characters": [],
        "replan_count": 1, "context": {"short_context": [feedback]},
    })

    carried = [i for i in out["context"]["short_context"] if i.get("kind") == "replan_feedback"]
    assert carried == [feedback]
