"""seed 幂等测试（阶段 2 展示前端多本书数据源）。

断言 create_sample_books() 幂等：重复调用不重复建书；每本示例书有
设定（ProjectSettings）/人物（≥2）/剧情线（≥1），可支撑前端侧边栏多书切换。
"""

from __future__ import annotations

from sqlalchemy import text

from myink.db import new_session, tenant_session
from myink.seed import _SAMPLE_BOOKS, DEMO_USERNAME, create_sample_books


def test_sample_books_idempotent():
    """重复调用 create_sample_books 不产生新书。"""
    # 首次调用可能是建书（若 init 早于补建）也可能已存在（幂等跳过）
    created = create_sample_books()
    second = create_sample_books()
    assert second == [], f"第二次调用应不新建书，实际返回 {second}"


def test_sample_books_have_full_setting():
    """每本示例书都有设定/人物/剧情线（多书展示数据完整）。"""
    titles = [spec["title"] for spec in _SAMPLE_BOOKS]
    with new_session() as db:
        rows = db.execute(
            text(
                "SELECT p.id, p.title FROM projects p "
                "JOIN users u ON u.id = p.user_id WHERE u.username = :username"
            ),
            {"username": DEMO_USERNAME},
        ).all()
    by_title = {r.title: r.id for r in rows}
    assert set(by_title) >= set(titles), f"示例书都应存在（init 会幂等补建），现有 {sorted(by_title)}"

    for title, pid in by_title.items():
        if title not in titles:
            continue  # 只校验示例书，demo 主书不在 _SAMPLE_BOOKS 里
        with tenant_session(str(pid)) as tdb:
            settings = tdb.execute(
                text("SELECT 1 FROM project_settings WHERE project_id=:p"), {"p": str(pid)}
            ).first()
            assert settings is not None, f"{title} 缺 ProjectSettings"
            chars = tdb.execute(
                text("SELECT count(*) FROM characters WHERE project_id=:p"), {"p": str(pid)}
            ).scalar()
            assert chars >= 2, f"{title} 人物数不足：{chars}"
            threads = tdb.execute(
                text("SELECT count(*) FROM plot_threads WHERE project_id=:p"), {"p": str(pid)}
            ).scalar()
            assert threads >= 1, f"{title} 缺剧情线"
