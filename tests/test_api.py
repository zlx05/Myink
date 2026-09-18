"""Python API 读端点测试（2026-08-09，前端接入暴露的契约回归）。

list_chapters 曾引用 Chapter 模型不存在的 target_words → 真实 API 500
（Go 网关测试用假 Python 服务测不到，前端接真 API 才暴露）。
回归：章节列表返回可序列化、字段合法。

list_projects 在阶段 3 加身份过滤（§14.1 ③，应用层按 X-Myink-User 归属断言），
改为 TestClient 带 demo 身份头走 HTTP 层；list_chapters 归属断言用 route-level
dependencies（不改函数签名），直接调用测试照常。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from myink.api.main import app, list_chapters
from myink.db import new_session
from myink.models import Chapter, User

client = TestClient(app)


def test_list_projects_returns_books():
    """项目列表按身份过滤：demo 用户带 X-Myink-User 头 → 返回自己的多本（示例书已补建）。"""
    with new_session() as db:
        user = db.query(User).filter(User.username == "demo").first()
        assert user is not None, "请先运行 `myink init`（demo 用户未建）"
    resp = client.get("/internal/v1/projects", headers={"X-Myink-User": str(user.id)})
    assert resp.status_code == 200
    projects = resp.json()
    assert len(projects) >= 3, f"应有多本（示例书已补建），实际 {len(projects)}"
    titles = {p["title"] for p in projects}
    assert "九州问天" in titles and "长安夜行" in titles and "星舰远征" in titles


def test_list_chapters_fields_valid(project_id):
    """章节列表字段合法（回归：target_words 曾致 500）+ word_count 字数口径（§6.9）。"""
    chapters = list_chapters(project_id)
    assert isinstance(chapters, list)
    for c in chapters:
        assert "chapter_seq" in c and "status" in c and "id" in c
        assert "target_words" not in c, "target_words 是 Project 字段，章节列表不应携带"
        assert c["word_count"] is not None and c["word_count"] >= 0, "每章应返回字数"
    if chapters:
        # 口径 = len(content) 字符数（与 L1 chapter_length_check 同源）；
        # chapters 是 RLS 租户表，须用 tenant_session 才能查到行
        from myink.db import tenant_session
        with tenant_session(project_id) as db:
            row = db.query(Chapter).filter(
                Chapter.project_id == uuid.UUID(project_id),
                Chapter.chapter_seq == chapters[0]["chapter_seq"],
            ).first()
            assert chapters[0]["word_count"] == len(row.content or ""), "字数 = 正文字符数"
