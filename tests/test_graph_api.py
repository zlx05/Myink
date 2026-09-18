"""关系图谱端点（§9 世界拓扑）：4 类节点 + 人物关系/地点层级边。

- 全量：建书设定即入图（含孤立项）；人物关系含失效行（valid_to 非空 → expired=True）；
- 地点层级：Location.parent_id 非空 → hierarchy 边（子→父）；
- 空项目 → nodes/edges 空列表（不 500）；
- 404/403 矩阵（仿 test_auth.py 归属断言口径）。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from myink.api.main import app
from myink.db import new_session, tenant_session
from myink.models import (Chapter, Character, Entity, Faction, Foreshadow,
                          Location, Project, Relation, User)

client = TestClient(app)


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `myink init`（demo 用户未建）"
        return u.id


def _h(uid) -> dict:
    """请求头：X-Myink-User = 网关已验证的 JWT sub（None → 不带，测 fail closed）。"""
    return {"X-Myink-User": str(uid)} if uid is not None else {}


def _seed_graph(pid: str) -> None:
    """种子：2 人物 + 1 势力 + 1 活跃关系 + 1 失效关系 + 2 地点（含父子层级）+ 1 实体。"""
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        a = Character(project_id=p, name="林晚", race="人族", realm_cap="金丹",
                      personality="谨慎隐忍", base_attrs={})
        b = Character(project_id=p, name="沈岳", race="妖族", realm_cap="元婴",
                      personality="桀骜", base_attrs={})
        db.add_all([a, b])
        db.flush()
        db.add(Faction(project_id=p, name="青云宗", stance="正道"))
        loc_root = Location(project_id=p, name="青云州")
        db.add(loc_root)
        db.flush()
        db.add(Location(project_id=p, name="青云山", parent_id=loc_root.id))
        db.add(Entity(project_id=p, entity_type="item", canonical_name="焚天剑",
                      properties={"description": "上古凶兵"}))
        db.add_all([
            # 活跃关系（valid_to IS NULL）：hostile，第 3 章
            Relation(project_id=p, source_id=a.id, target_id=b.id, relation_type="hostile",
                     source_chapter=3, confidence=0.9, valid_from=1),
            # 失效关系（valid_to 非空）：knows，第 5 章建立、第 7 章失效
            Relation(project_id=p, source_id=b.id, target_id=a.id, relation_type="knows",
                     source_chapter=8, confidence=0.7, valid_from=5, valid_to=7),
        ])
        db.commit()


def _graph(pid: str, uid=None) -> tuple[int, dict]:
    resp = client.get(f"/internal/v1/projects/{pid}/graph", headers=_h(uid or _demo_user_id()))
    return resp.status_code, resp.json()


# ---- 全量拓扑 ----


def test_graph_full_topology(temp_project):
    pid = temp_project
    _seed_graph(pid)
    status, data = _graph(pid)
    assert status == 200
    # 4 类节点齐全（含孤立势力：无关系边也入图）
    assert {n["type"] for n in data["nodes"]} == {"character", "faction", "location", "entity"}
    assert len(data["nodes"]) == 6  # 2 人物 + 1 势力 + 2 地点 + 1 实体
    # 人物关系 2 条（1 活跃 + 1 失效）+ 地点层级 1 条
    relations = [e for e in data["edges"] if e["edge_type"] != "hierarchy"]
    hierarchies = [e for e in data["edges"] if e["edge_type"] == "hierarchy"]
    assert len(relations) == 2 and len(hierarchies) == 1
    active = [e for e in relations if not e["expired"]]
    expired = [e for e in relations if e["expired"]]
    assert len(active) == 1 and len(expired) == 1
    # 活跃关系带置信度/章号；失效关系标记 expired（valid_to 非空）
    assert active[0]["confidence"] == 0.9 and active[0]["source_chapter"] == 3
    assert expired[0]["source_chapter"] == 8 and expired[0]["confidence"] == 0.7
    # 层级边子→父（source=子地点，target=父地点 id），无章号/置信度
    assert hierarchies[0]["source_chapter"] is None and hierarchies[0]["confidence"] is None
    loc_ids = {n["id"]: n for n in data["nodes"] if n["type"] == "location"}
    assert hierarchies[0]["target_id"] in loc_ids


def test_graph_node_fields(temp_project):
    pid = temp_project
    _seed_graph(pid)
    _, data = _graph(pid)
    by_type = {n["type"]: n for n in data["nodes"]}
    # 人物带境界、势力带立场、地点带 parent_id、实体带 entity_type
    assert by_type["character"]["realm_cap"] in ("金丹", "元婴")
    assert by_type["faction"]["stance"] == "正道"
    assert by_type["location"]["parent_id"] is not None or any(
        n["type"] == "location" and n["parent_id"] is None for n in data["nodes"])
    assert by_type["entity"]["entity_type"] == "item"


def test_graph_unifies_location_entity_and_registry(temp_project):
    """地点节点 = Location ∪ Entity(location) 按名去重；实体桶不再重复出地点节点。"""
    p = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        reg = Location(project_id=p, name="青云州")
        db.add(reg)
        db.flush()
        # 旧数据：只有 Entity 行、没有 Location 行的地点实体 → 仍应出现在 location 节点
        db.add(Entity(project_id=p, entity_type="location", canonical_name="后山北峰",
                      properties={}))
        # 同名 Location + Entity(location) → 只出一个 location 节点（去重）
        db.add(Entity(project_id=p, entity_type="location", canonical_name="青云州",
                      properties={}))
        db.commit()
    _, data = _graph(temp_project)
    locs = [n for n in data["nodes"] if n["type"] == "location"]
    ents = [n for n in data["nodes"] if n["type"] == "entity"]
    assert {n["name"] for n in locs} == {"青云州", "后山北峰"}, "Location ∪ Entity(location) 按名去重"
    assert all(n["entity_type"] != "location" for n in ents), "实体桶不再重复出地点实体"


def test_persist_location_entity_mirrors_with_parent(temp_project):
    """new_entity 地点候选 → Entity 落库 + 镜像进 Location 注册表并挂 parent_id（§9 层级）。"""
    from myink.workflow import nodes

    cands = [
        {"kind": "new_entity", "source_chapter": 1, "confidence": 0.9,
         "payload": {"entity_type": "location", "name": "后山", "description": ""}},
        {"kind": "new_entity", "source_chapter": 1, "confidence": 0.9,
         "payload": {"entity_type": "location", "name": "北峰洞口", "description": "",
                     "parent": "后山"}},
    ]
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 1, cands, skip_pool_handled=False)
        db.commit()
    with tenant_session(temp_project) as db:
        rows = {l.name: l for l in db.query(Location).all()}
        assert set(rows) == {"后山", "北峰洞口"}, "地点实体应镜像进 Location 注册表"
        assert rows["北峰洞口"].parent_id == rows["后山"].id, "parent 应按名解析并挂 parent_id"
        ents = db.query(Entity).all()
        assert {e.canonical_name for e in ents} == {"后山", "北峰洞口"}, "Entity 落库不受影响"


# ---- 空项目 ----


def test_graph_empty_project(temp_project):
    status, data = _graph(temp_project)
    assert status == 200
    assert data == {"nodes": [], "edges": []}


# ---- 404 / 403 矩阵（require_owner 挂依赖，走 TestClient HTTP 层）----


def test_graph_missing_project_404():
    resp = client.get(f"/internal/v1/projects/{uuid.uuid4()}/graph", headers=_h(_demo_user_id()))
    assert resp.status_code == 404


def test_graph_rejects_foreign_user(temp_project):
    resp = client.get(f"/internal/v1/projects/{temp_project}/graph", headers=_h(uuid.uuid4()))
    assert resp.status_code == 403


def test_graph_fail_closed_without_identity(temp_project):
    resp = client.get(f"/internal/v1/projects/{temp_project}/graph")
    assert resp.status_code == 403


def test_graph_rejects_invalid_identity(temp_project):
    resp = client.get(f"/internal/v1/projects/{temp_project}/graph", headers=_h("not-a-uuid"))
    assert resp.status_code == 403


# ---- 伏笔池台账（§7.9 状态机全量）----


def _seed_foreshadows(pid: str) -> None:
    """种子：1 开放（planted）+ 1 已回收（resolved，保留历史）。"""
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        db.add_all([
            Foreshadow(project_id=p, description="玉佩碎片的来历",
                       status="planted", planted_chapter=2,
                       trigger={"actor": "林尘", "action": "寻回", "object": "玉佩"}),
            Foreshadow(project_id=p, description="金色旋涡的隐秘",
                       status="resolved", planted_chapter=1, resolved_chapter=5),
        ])
        db.commit()


def test_foreshadows_ledger_all_statuses(temp_project):
    """全状态返回（planted/developing/resolved/dropped 都展示），含 trigger/回收章。"""
    pid = temp_project
    _seed_foreshadows(pid)
    resp = client.get(f"/internal/v1/projects/{pid}/foreshadows", headers=_h(_demo_user_id()))
    assert resp.status_code == 200
    rows = resp.json()
    by_desc = {r["description"]: r for r in rows}
    assert set(by_desc) == {"玉佩碎片的来历", "金色旋涡的隐秘"}
    open_f = by_desc["玉佩碎片的来历"]
    assert open_f["status"] == "planted" and open_f["planted_chapter"] == 2
    assert open_f["resolved_chapter"] is None
    assert open_f["trigger"].get("actor") == "林尘"
    resolved = by_desc["金色旋涡的隐秘"]
    assert resolved["status"] == "resolved" and resolved["resolved_chapter"] == 5


def test_foreshadows_empty_project(temp_project):
    resp = client.get(f"/internal/v1/projects/{temp_project}/foreshadows",
                      headers=_h(_demo_user_id()))
    assert resp.status_code == 200 and resp.json() == []


def test_foreshadows_auth_matrix(temp_project):
    _seed_foreshadows(temp_project)
    assert client.get(f"/internal/v1/projects/{temp_project}/foreshadows").status_code == 403
    assert client.get(f"/internal/v1/projects/{temp_project}/foreshadows",
                      headers=_h(uuid.uuid4())).status_code == 403
    assert client.get(f"/internal/v1/projects/{temp_project}/foreshadows",
                      headers=_h("not-a-uuid")).status_code == 403
    assert client.get(f"/internal/v1/projects/{uuid.uuid4()}/foreshadows",
                      headers=_h(_demo_user_id())).status_code == 404


# ---- 章节列表含摘要（§7 短期记忆，前端章节记忆区块）----


def test_chapter_list_includes_summary(temp_project):
    """章节列表返回 summary（短期记忆展示）；无摘要章 → None 不崩。"""
    p = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        db.add_all([
            Chapter(project_id=p, chapter_seq=1, status="confirmed",
                    content="正文一", summary="林尘得玉佩，初入青云宗。"),
            Chapter(project_id=p, chapter_seq=2, status="confirmed",
                    content="正文二", summary=None),
        ])
        db.commit()
    resp = client.get(f"/internal/v1/projects/{temp_project}/chapters",
                      headers=_h(_demo_user_id()))
    assert resp.status_code == 200
    by_seq = {c["chapter_seq"]: c for c in resp.json()}
    assert by_seq[1]["summary"] == "林尘得玉佩，初入青云宗。"
    assert by_seq[2]["summary"] is None
    assert by_seq[1]["word_count"] == 3
