"""JWT 身份断言测试（§14.1 ③ 归属校验双保险 / §14.5 越权测试矩阵）。

- 签发：username → JWT（HS256，sub=user_id）；未知用户 404。
- 归属断言（require_owner）：网关透传 X-Myink-User（已验证身份）→ project.user_id 匹配才放行。
  跨用户读 → 403（伪造身份）；缺失身份 → 403（fail closed）；身份非法 → 403；项目不存在 → 404。
- list_projects：按身份过滤（只返回自己的书）；无身份 → 空列表（fail closed）。

信任链：网关验 JWT（Go 侧测试覆盖）→ 透传 X-Myink-User → 本文件测 Python 侧断言。
"""

from __future__ import annotations

import uuid

import jwt
from fastapi.testclient import TestClient

from myink.api.main import app
from myink.config import settings
from myink.db import new_session
from myink.models import Project, User

client = TestClient(app)


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `myink init`（demo 用户未建）"
        return u.id


def _h(uid: str | uuid.UUID | None) -> dict:
    """请求头：X-Myink-User = 网关已验证的 JWT sub（None → 不带，测 fail closed）。"""
    return {"X-Myink-User": str(uid)} if uid is not None else {}


# ---- 签发 ----


def test_issue_token_demo():
    resp = client.post("/internal/v1/auth/token", json={"username": "demo"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == str(_demo_user_id())
    claims = jwt.decode(data["token"], settings.jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == str(_demo_user_id())
    assert claims["iss"] == "myink"


def test_issue_token_unknown_user_404():
    resp = client.post("/internal/v1/auth/token", json={"username": "nobody"})
    assert resp.status_code == 404


def test_demo_login_disabled_in_production(monkeypatch):
    from types import SimpleNamespace
    import myink.api.auth as auth
    monkeypatch.setattr(auth, "settings", SimpleNamespace(is_prod=lambda: True))
    resp = client.post("/internal/v1/auth/token", json={"username": "demo"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "DEMO_LOGIN_DISABLED"


# ---- 归属断言（§14.5 越权矩阵：跨用户读 / 伪装 project_id / 漏传身份）----


def test_owner_access_own_project(temp_project):
    """demo 身份访问自己的书 → 放行。"""
    resp = client.get(f"/internal/v1/projects/{temp_project}/chapters", headers=_h(_demo_user_id()))
    assert resp.status_code == 200


def test_owner_rejects_foreign_project(temp_project):
    """伪造他人身份访问此书（项目属 demo）→ 403（伪装 project_id / 跨用户读）。"""
    resp = client.get(f"/internal/v1/projects/{temp_project}/chapters", headers=_h(uuid.uuid4()))
    assert resp.status_code == 403


def test_owner_fail_closed_without_identity(temp_project):
    """缺失身份头 → 403（fail closed，不默认放行）。"""
    resp = client.get(f"/internal/v1/projects/{temp_project}/chapters")
    assert resp.status_code == 403


def test_owner_rejects_invalid_identity(temp_project):
    """身份头非法 uuid → 403（不 500，评审 A4 口径）。"""
    resp = client.get(f"/internal/v1/projects/{temp_project}/chapters", headers=_h("not-a-uuid"))
    assert resp.status_code == 403


def test_owner_missing_project_404():
    """项目不存在 → 404（资源不存在）；存在但不属于请求者 → 403 已由越权用例覆盖。"""
    resp = client.get(f"/internal/v1/projects/{uuid.uuid4()}/chapters", headers=_h(_demo_user_id()))
    assert resp.status_code == 404


# ---- list_projects 按身份过滤（§14.1 ③，根表无 RLS → 应用层第二道门）----


def test_list_projects_only_own():
    """只返回自己的书：建他人用户+书，demo 身份看不到。"""
    other = None
    try:
        with new_session() as db:
            other = User(username=f"other-{uuid.uuid4().hex[:6]}")
            db.add(other)
            db.flush()
            db.add(Project(user_id=other.id, title="他人书"))
            db.commit()
        resp = client.get("/internal/v1/projects", headers=_h(_demo_user_id()))
        assert resp.status_code == 200
        titles = {p["title"] for p in resp.json()}
        assert "九州问天" in titles and "他人书" not in titles
    finally:
        if other is not None:
            with new_session() as db:
                db.query(Project).filter(Project.user_id == other.id).delete()
                db.query(User).filter(User.id == other.id).delete()
                db.commit()


def test_list_projects_fail_closed_without_identity():
    """缺失身份 → 空列表（fail closed，不泄露全部作品）。"""
    resp = client.get("/internal/v1/projects")
    assert resp.status_code == 200
    assert resp.json() == []
