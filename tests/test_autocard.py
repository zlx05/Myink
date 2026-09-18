"""正文自动建档测试（§7.11 ④：新人物卡片确认落卡 / 武器技能地点自动建档）。

范围：
- node_persist：出现 character_card 候选 → 池分流（章节 awaiting_review + 候选进池），
  new_entity 即使在本路径也自动落 entities（低风险自动建档，仿 plotline 口径）；
- confirm_candidate：确认 character_card → characters 静态基底 + alias 归一化，重复名跳过；
- new_entity 自动落库去重（同 entity_type+name 不重复建）；
- GET /projects/{pid}/entities：读回形状 + 越权矩阵。

注意：persist 内事务（SET LOCAL tenant 上下文 commit 即失效），断言须在同一 tenant_session。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from myink.api.main import app
from myink.db import new_session, tenant_session
from myink.models import Alias, Character, Entity, MemoryCandidate
from myink.workflow import nodes

client = TestClient(app)


def _demo_user_id() -> uuid.UUID:
    from myink.models import User
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `myink init`（demo 用户未建）"
        return u.id


def _h(uid: str | uuid.UUID | None) -> dict:
    return {"X-Myink-User": str(uid)} if uid is not None else {}


_CARD = {"name": "沈青", "identity": "青云宗大师姐", "role": "主角师姐",
         "personality": "外冷内热", "importance": "中期领路人"}
_ENTITY = {"entity_type": "item", "name": "诛仙剑", "description": "上古神兵"}


def _count(db, model, **kw) -> int:
    q = db.query(model)
    for k, v in kw.items():
        q = q.filter(getattr(model, k) == v)
    return q.count()


def _pool_path_state(pid: str) -> dict:
    return {
        "project_id": pid,
        "chapter_seq": 1,
        "task_id": None,
        "report": {"summary": {"critical": 0, "l2_major": 0}},
        "candidates": [
            {"kind": "character_card", "payload": dict(_CARD), "confidence": 0.85},
            {"kind": "new_entity", "payload": dict(_ENTITY), "confidence": 0.9},
        ],
    }


def test_character_card_routes_to_pool_and_entity_auto_persists(temp_project):
    """出现新人物卡片 → 池分流（章节 awaiting_review）；新实体即使在本路径也自动落库。"""
    result = nodes.node_persist(_pool_path_state(temp_project))
    assert result["needs_review"] is True, "出现 character_card 必须转人工确认"
    with tenant_session(temp_project) as db:
        assert _count(db, Entity, project_id=uuid.UUID(temp_project),
                      entity_type="item", canonical_name="诛仙剑") == 1, \
            "new_entity 低风险应在池路径也自动建档"
        card = db.query(MemoryCandidate).filter_by(kind="character_card",
                                                   project_id=uuid.UUID(temp_project)).first()
        assert card is not None and card.status == "pending", "人物卡片候选应进待确认池"
        assert card.payload.get("name") == "沈青"
        assert _count(db, Character, name="沈青") == 0, "未确认前不应建卡"


def test_confirm_character_card_writes_character_and_alias(temp_project):
    nodes.node_persist(_pool_path_state(temp_project))
    with tenant_session(temp_project) as db:
        cand = db.query(MemoryCandidate).filter_by(kind="character_card",
                                                   project_id=uuid.UUID(temp_project)).first()
        confirmed = nodes.confirm_candidate(db, temp_project, cand.id)
        assert confirmed is not None and confirmed.status == "confirmed"
        db.flush()  # autoflush=False（§5.2）：显式 flush 对齐生产 commit，alias 可见
        ch = db.query(Character).filter_by(project_id=uuid.UUID(temp_project), name="沈青").first()
        assert ch is not None, "确认后应建人物卡片"
        assert ch.realm_cap == "无"          # 未设定境界默认「无」（列非空）
        assert ch.personality == "外冷内热"
        assert ch.base_attrs.get("identity") == "青云宗大师姐"
        assert ch.base_attrs.get("role") == "主角师姐"
        assert db.query(Alias).filter_by(project_id=uuid.UUID(temp_project),
                                         alias="沈青", entity_id=ch.id).first() is not None, \
            "建卡应同时写 alias 归一化（§7.5）"
        # 幂等：同一候选不可重复确认
        assert nodes.confirm_candidate(db, temp_project, cand.id) is None


def test_character_card_dedup_skips_existing_name(temp_project):
    """重复名不重复建卡：已建档人物再落 character_card → skip。"""
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 1,
                                  [{"kind": "character_card", "payload": dict(_CARD),
                                    "confidence": 0.8}], skip_pool_handled=False)
        assert _count(db, Character, name="沈青") == 1
        # 跨章再次出现同名 → 不重复建卡
        nodes._persist_candidates(db, temp_project, 2,
                                  [{"kind": "character_card", "payload": dict(_CARD),
                                    "confidence": 0.9}], skip_pool_handled=False)
        assert _count(db, Character, name="沈青") == 1, "已建档人物不应重复建卡"


def test_character_card_dedup_via_alias(temp_project):
    """跨别名去重（§7.5 别名解析接线，审计 Medium-1）：正文以别名出现不再重复建卡。"""
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 1,
                                  [{"kind": "character_card", "payload": dict(_CARD),
                                    "confidence": 0.8}], skip_pool_handled=False)
        ch = db.query(Character).filter_by(name="沈青").first()
        assert ch is not None
        # 模拟既有别名行（正文曾以「沈大师姐」称呼 → 已归一）
        db.add(Alias(project_id=uuid.UUID(temp_project), alias="沈大师姐", entity_id=ch.id))
        db.flush()
        # 后续章以别名出卡 → get_character 别名解析命中 → 不重复建卡
        nodes._persist_candidates(db, temp_project, 2,
                                  [{"kind": "character_card",
                                    "payload": {"name": "沈大师姐", "identity": "青云宗大师姐"},
                                    "confidence": 0.9}], skip_pool_handled=False)
        assert _count(db, Character, name="沈青") == 1, "别名重复出卡应归一到已有卡片"
        assert _count(db, Character, name="沈大师姐") == 0


def test_new_entity_invalid_type_skipped(temp_project):
    """entity_type 白名单（审计 Low-3）：LLM 输出白名单外类型 → 不落库（否则设定页不可见）。"""
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 1,
                                  [{"kind": "new_entity",
                                    "payload": {"entity_type": "神器", "name": "诛仙剑"},
                                    "confidence": 0.9}], skip_pool_handled=True)
        assert _count(db, Entity, canonical_name="诛仙剑") == 0, "白名单外类型不应登记"


def _stub_llm(monkeypatch, payload):
    """mock nodes._llm：返回固定 extract JSON，不真调 LLM（make_chain 仅构造成员无网络）。"""
    import json
    from types import SimpleNamespace

    from myink.workflow import nodes

    def fake_llm(db, state, node, role, chain, messages, **kw):
        return SimpleNamespace(error=None, content=json.dumps(payload, ensure_ascii=False)), []

    monkeypatch.setattr(nodes, "_llm", fake_llm)
    return nodes


def test_extract_character_card_drops_existing_name(temp_project, monkeypatch):
    """抽取侧去重（§7.11 ④，防 resume 死循环）：正文已建档人物再出卡 → 候选被丢弃。

    缺此守卫时 resume 重跑会把已确认角色反复抽成新卡 → persist 恒 has_card → 章节
    永远 awaiting_review、正文永远不落库。落库侧（_persist_candidates）去重已有，但
    进池前必须拦掉，否则 persist 的 has_card 检查仍会暂停。
    """
    with tenant_session(temp_project) as db:
        db.add(Character(project_id=uuid.UUID(temp_project), name="沈青", realm_cap="金丹"))
        db.flush()
    nodes = _stub_llm(monkeypatch, {"candidates": [
        {"kind": "character_card", "source_chapter": 1, "payload": dict(_CARD), "confidence": 0.9},
        {"kind": "new_entity", "source_chapter": 1, "payload": dict(_ENTITY), "confidence": 0.8},
    ]})
    with tenant_session(temp_project) as db:
        cands, err = nodes.extract_candidates_from_draft(
            db, project_id=temp_project, chapter_seq=1, draft="正文")
        assert err is None
        assert [c["kind"] for c in cands] == ["new_entity"], \
            f"已建档名字沈青不应再抽成新卡: {cands}"


def test_extract_character_card_keeps_new_name(temp_project, monkeypatch):
    """未建档新名字正常出卡（不误杀）：新人物卡片候选应保留进待确认池。"""
    nodes = _stub_llm(monkeypatch, {"candidates": [
        {"kind": "character_card", "source_chapter": 1, "payload": dict(_CARD), "confidence": 0.9},
    ]})
    with tenant_session(temp_project) as db:
        cands, err = nodes.extract_candidates_from_draft(
            db, project_id=temp_project, chapter_seq=1, draft="正文")
        assert err is None
        assert len(cands) == 1 and cands[0]["kind"] == "character_card"


def test_empty_name_card_does_not_block_auto(temp_project):
    """空名卡片（审计 Low-4）：不触发池分流、不建卡，章节照常 auto 落库。"""
    state = {
        "project_id": temp_project,
        "chapter_seq": 1,
        "task_id": None,
        "report": {"summary": {"critical": 0, "l2_major": 0}},
        "draft": "正文内容",
        "candidates": [{"kind": "character_card", "payload": {"name": "  "}, "confidence": 0.8}],
    }
    result = nodes.node_persist(state)
    assert result["needs_review"] is False, "空名卡片不应把章节拖进确认池"


def test_new_entity_auto_persist_dedup(temp_project):
    """正常 auto 路径 new_entity 自动落库 + 同 entity_type+name 去重。"""
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 1,
                                  [{"kind": "new_entity", "payload": dict(_ENTITY),
                                    "confidence": 0.9}], skip_pool_handled=True)
        assert _count(db, Entity, entity_type="item", canonical_name="诛仙剑") == 1
        ent = db.query(Entity).filter_by(entity_type="item", canonical_name="诛仙剑").first()
        assert ent.properties.get("description") == "上古神兵"
        assert ent.properties.get("first_seen_chapter") == 1
        # 去重：同类型同名不重复建
        nodes._persist_candidates(db, temp_project, 2,
                                  [{"kind": "new_entity", "payload": dict(_ENTITY),
                                    "confidence": 0.95}], skip_pool_handled=True)
        assert _count(db, Entity, entity_type="item", canonical_name="诛仙剑") == 1


def test_list_entities_endpoint(temp_project):
    """GET /projects/{pid}/entities：读回自动建档的实体卡 + 越权矩阵。"""
    uid = _demo_user_id()
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 1,
                                  [{"kind": "new_entity", "payload": dict(_ENTITY),
                                    "confidence": 0.9}], skip_pool_handled=True)
    resp = client.get(f"/internal/v1/projects/{temp_project}/entities", headers=_h(uid))
    assert resp.status_code == 200
    cards = resp.json()
    assert len(cards) == 1
    assert cards[0]["entity_type"] == "item"
    assert cards[0]["name"] == "诛仙剑"
    assert cards[0]["description"] == "上古神兵"
    assert cards[0]["first_seen_chapter"] == 1
    assert cards[0]["id"]
    # 越权矩阵
    assert client.get(f"/internal/v1/projects/{temp_project}/entities").status_code == 403
    assert client.get(f"/internal/v1/projects/{temp_project}/entities",
                      headers=_h(uuid.uuid4())).status_code == 403
