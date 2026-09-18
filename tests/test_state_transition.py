"""角色累计状态必须物化为可直接落库的完整值。"""

import uuid

from myink.db import tenant_session
from myink.models import Character, CharacterState
from myink.workflow import nodes
from myink.workflow.nodes import _materialize_cumulative_state


def test_incremental_item_state_is_merged_with_ledger_value() -> None:
    old = "持有玉佩碎片、纸条、两块仿制玉片"
    new = "新增怪人摊主所给一包黑褐色粗药渣；其余物件不变"

    assert _materialize_cumulative_state("item", old, new) == (
        "持有玉佩碎片、纸条、两块仿制玉片；新增怪人摊主所给一包黑褐色粗药渣"
    )


def test_non_cumulative_goal_is_not_merged() -> None:
    assert _materialize_cumulative_state("goal", "查圆形钥匙", "先调查吊坠") == "先调查吊坠"


def test_explicit_item_removal_is_not_blindly_appended() -> None:
    new = "交出纸条，仍持有玉佩碎片"
    assert _materialize_cumulative_state("item", "持有玉佩碎片、纸条", new) == new


def test_persist_rechecks_current_ledger_and_merges_old_incremental_candidate(temp_project: str) -> None:
    """确认旧候选时以实时台账为准，不能用模型携带的残缺 old_value 覆盖状态。"""
    with tenant_session(temp_project) as db:
        character = Character(
            project_id=uuid.UUID(temp_project), name="林尘", realm_cap="筑基",
        )
        db.add(character)
        db.flush()
        db.add(CharacterState(
            project_id=uuid.UUID(temp_project), character_id=character.id,
            chapter_seq=16, field="item", old_value=None,
            new_value="持有玉佩碎片、纸条", source_chapter=16,
        ))
        db.flush()

        nodes._persist_candidates(db, temp_project, 17, [{
            "kind": "character_state",
            "payload": {
                "character_id": str(character.id), "field": "item",
                "old_value": "错误的模型旧值",
                "new_value": "新增一包药渣；其余物件不变",
                "confidence": 0.85,
            },
        }])
        db.flush()

        row = db.query(CharacterState).filter_by(
            project_id=uuid.UUID(temp_project), character_id=character.id,
            chapter_seq=17, field="item",
        ).one()
        assert row.old_value == "持有玉佩碎片、纸条"
        assert row.new_value == "持有玉佩碎片、纸条；新增一包药渣"
