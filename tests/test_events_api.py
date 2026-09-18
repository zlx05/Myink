"""事件台账与人物状态历史端点（§7.4 中期记忆 / §7.7 追加式台账）。

这两块数据此前只写不读：事件表有数据但无路由，人物状态底层是完整追加式台账
却被 get_character_state 压平成「当前值」。本套件守住读出来的形状：

- 事件：participants 落库是 canonical 人物 UUID，端点须翻成**人名**；按章降序；
  from_chapter/to_chapter 区间过滤生效；空项目 → []（不 500）；
- 状态历史：返回 character_states **原始追加行** —— 含 valid_to 非空的已失效行，
  正序；字段（old_value/source_chapter/confidence/valid_to）一个都不能少；
- 404/403 矩阵 + 非法 character_id → 400（require_owner 只校验 project_id）。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from aiink.api.main import app
from aiink.db import new_session, tenant_session
from aiink.models import Character, CharacterState, Event, User

client = TestClient(app)


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `aiink init`（demo 用户未建）"
        return u.id


def _h(uid) -> dict:
    """请求头：X-AiInk-User = 网关已验证的 JWT sub（None → 不带，测 fail closed）。"""
    return {"X-AiInk-User": str(uid)} if uid is not None else {}


def _seed_events(pid: str) -> dict[str, str]:
    """种子：2 人物 + 3 事件（含 1 条参与者指向已删角色 + 1 条空参与者）。"""
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        a = Character(project_id=p, name="林晚", aliases=[], race="人族", realm_cap="金丹",
                      personality="谨慎隐忍", base_attrs={})
        b = Character(project_id=p, name="沈岳", aliases=[], race="妖族", realm_cap="元婴",
                      personality="桀骜", base_attrs={})
        db.add_all([a, b])
        db.flush()
        db.add_all([
            Event(project_id=p, summary="林晚于青云山夺剑", participants=[str(a.id)],
                  source_chapter=3, confidence=0.9, promoted_to_fact=True, timeline="第一日"),
            Event(project_id=p, summary="沈岳夜访秘境", participants=[str(b.id)],
                  source_chapter=7, confidence=0.8, timeline=None),
            # 参与者指向已删角色（不在 characters 表）→ 端点须丢弃而不是回吐裸 uuid
            Event(project_id=p, summary="无名者现身", participants=[str(uuid.uuid4())],
                  source_chapter=9, confidence=0.5),
        ])
        db.commit()
        return {"lin": str(a.id), "shen": str(b.id)}


def _events(pid: str, uid=None, **params) -> tuple[int, list]:
    resp = client.get(f"/internal/v1/projects/{pid}/events", headers=_h(uid or _demo_user_id()),
                      params=params)
    return resp.status_code, resp.json()


# ---- 事件台账（§7.4）----


def test_events_participants_resolved_to_names(temp_project):
    """participants 须翻成人名，而不是回吐 canonical uuid。"""
    pid = temp_project
    _seed_events(pid)
    status, rows = _events(pid)
    assert status == 200
    by_summary = {r["summary"]: r for r in rows}
    assert by_summary["林晚于青云山夺剑"]["participants"] == ["林晚"]
    assert by_summary["沈岳夜访秘境"]["participants"] == ["沈岳"]
    # 指向已删角色的参与者被丢弃（不泄漏裸 uuid）
    assert by_summary["无名者现身"]["participants"] == []


def test_events_ordered_desc_and_range_filter(temp_project):
    """按章号降序（台账回看，最近在前）；from_chapter/to_chapter 区间过滤生效。"""
    pid = temp_project
    _seed_events(pid)
    _, rows = _events(pid)
    assert [r["source_chapter"] for r in rows] == [9, 7, 3]

    _, only_mid = _events(pid, from_chapter=7, to_chapter=7)
    assert [r["source_chapter"] for r in only_mid] == [7]

    _, head = _events(pid, to_chapter=5)
    assert [r["source_chapter"] for r in head] == [3]

    _, tail = _events(pid, from_chapter=7)
    assert [r["source_chapter"] for r in tail] == [9, 7]


def test_events_expose_ledger_fields(temp_project):
    """台账字段齐全（promoted_to_fact / confidence / timeline）。"""
    pid = temp_project
    _seed_events(pid)
    _, rows = _events(pid)
    top = rows[0]
    assert set(top) == {"id", "summary", "participants", "location_id", "timeline",
                        "source_chapter", "confidence", "promoted_to_fact"}
    promoted = next(r for r in rows if r["summary"] == "林晚于青云山夺剑")
    assert promoted["promoted_to_fact"] is True and promoted["confidence"] == 0.9
    assert promoted["timeline"] == "第一日"


def test_events_empty_project(temp_project):
    status, rows = _events(temp_project)
    assert status == 200 and rows == []


def test_events_auth_matrix(temp_project):
    _seed_events(temp_project)
    assert client.get(f"/internal/v1/projects/{temp_project}/events").status_code == 403
    assert client.get(f"/internal/v1/projects/{temp_project}/events",
                      headers=_h(uuid.uuid4())).status_code == 403
    assert client.get(f"/internal/v1/projects/{temp_project}/events",
                      headers=_h("not-a-uuid")).status_code == 403
    assert client.get(f"/internal/v1/projects/{uuid.uuid4()}/events",
                      headers=_h(_demo_user_id())).status_code == 404


# ---- 人物状态历史（§7.7）----


def _seed_states(pid: str) -> str:
    """种子：同一 field 三次跃迁，其中第 2 条已失效（valid_to 非空）。

    第 3 条是当前有效行。get_character_state 会过滤掉已失效行并折叠成最新值，
    所以「失效行也在」是本端点的核心断言。
    """
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        ch = Character(project_id=p, name="林晚", aliases=[], race="人族", realm_cap="金丹",
                       personality="谨慎隐忍", base_attrs={})
        db.add(ch)
        db.flush()
        cid = ch.id
        db.add_all([
            CharacterState(project_id=p, character_id=cid, chapter_seq=2, field="realm",
                           old_value="练气九层", new_value="筑基一层",
                           source_chapter=2, confidence=0.9, valid_from=2),
            # 已失效：第 3 章写入、第 5 章被随后一条取代
            CharacterState(project_id=p, character_id=cid, chapter_seq=3, field="realm",
                           old_value="筑基一层", new_value="筑基三层",
                           source_chapter=3, confidence=0.8, valid_from=3, valid_to=5),
            # 当前有效行（valid_to IS NULL）
            CharacterState(project_id=p, character_id=cid, chapter_seq=5, field="realm",
                           old_value="筑基三层", new_value="金丹初期",
                           source_chapter=5, confidence=0.95, valid_from=5),
        ])
        db.commit()
        return str(cid)


def _history(pid: str, cid: str, uid=None) -> tuple[int, list]:
    resp = client.get(f"/internal/v1/projects/{pid}/characters/{cid}/state-history",
                      headers=_h(uid or _demo_user_id()))
    return resp.status_code, resp.json()


def test_state_history_includes_invalidated_rows(temp_project):
    """核心断言：已失效行（valid_to 非空）也必须返回 —— 复用 get_character_state 会红。"""
    pid = temp_project
    cid = _seed_states(pid)
    status, rows = _history(pid, cid)
    assert status == 200
    assert len(rows) == 3
    # 正序时间线
    assert [r["chapter_seq"] for r in rows] == [2, 3, 5]
    assert [r["valid_to"] for r in rows] == [None, 5, None]


def test_state_history_carries_change_fields(temp_project):
    """old→new、来源章与置信度都在（压平前会丢的正是这些）。"""
    pid = temp_project
    cid = _seed_states(pid)
    _, rows = _history(pid, cid)
    second = rows[1]
    assert set(second) == {"field", "old_value", "new_value", "chapter_seq",
                           "source_chapter", "confidence", "valid_from", "valid_to"}
    assert second["old_value"] == "筑基一层" and second["new_value"] == "筑基三层"
    assert second["source_chapter"] == 3 and second["confidence"] == 0.8


def test_state_history_unknown_character_empty(temp_project):
    """合法 uuid 但无台账 → []（不是 404）。"""
    status, rows = _history(temp_project, str(uuid.uuid4()))
    assert status == 200 and rows == []


def test_state_history_invalid_character_id_400(temp_project):
    """character_id 不在 require_owner 校验范围内，须自守卫（否则查库才炸成 500）。"""
    resp = client.get(
        f"/internal/v1/projects/{temp_project}/characters/not-a-uuid/state-history",
        headers=_h(_demo_user_id()))
    assert resp.status_code == 400


def test_state_history_auth_matrix(temp_project):
    cid = _seed_states(temp_project)
    path = f"/internal/v1/projects/{temp_project}/characters/{cid}/state-history"
    assert client.get(path).status_code == 403
    assert client.get(path, headers=_h(uuid.uuid4())).status_code == 403
    assert client.get(path, headers=_h("not-a-uuid")).status_code == 403
    assert client.get(
        f"/internal/v1/projects/{uuid.uuid4()}/characters/{cid}/state-history",
        headers=_h(_demo_user_id())).status_code == 404
