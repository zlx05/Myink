"""章节历史版本（阶段 4 版本表：覆盖写前快照 → 列表 → 回退）。

语义（models/chapter.py ChapterVersion）：chapters 是「当前版本」，每次覆盖写前把旧状态
快照成历史行；回退 = 快照当前（revert 留痕）→ 覆盖回目标版本 → 版本 +1。
覆盖两条写路径：persist 的 save_chapter（批次/单章生成）与 PUT content 编辑端点。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from myink.api.routes_chapters import (ContentUpdate, list_chapter_versions,
                                       restore_chapter_version,
                                       update_chapter_content)
from myink.db import tenant_session
from myink.memory.repository import save_chapter, save_review_draft
from myink.models import Chapter, ChapterVersion


def _seed(pid: str, seq: int = 1) -> str:
    with tenant_session(pid) as db:
        ch = save_chapter(db, project_id=uuid.UUID(pid), chapter_seq=seq,
                          content="v1 正文", title="第一章")
        db.commit()
        return str(ch.id)


def test_save_chapter_snapshots_previous_versions(temp_project):
    """save_chapter 覆盖写（批次/生成落库）→ 旧版本进版本表，version 单调递增。"""
    cid = _seed(temp_project)
    with tenant_session(temp_project) as db:
        save_chapter(db, project_id=uuid.UUID(temp_project), chapter_seq=1,
                     content="v2 正文", title="第一章", generation_source="batch")
        db.commit()
    with tenant_session(temp_project) as db:
        ch = db.get(Chapter, uuid.UUID(cid))
        assert ch.version == 2 and ch.content == "v2 正文"
        hist = db.query(ChapterVersion).filter(ChapterVersion.chapter_id == ch.id).all()
        assert len(hist) == 1
        assert hist[0].version == 1 and hist[0].content == "v1 正文"
        assert hist[0].reason == "batch"


def test_review_draft_is_visible_and_final_confirmation_does_not_duplicate_version(temp_project):
    """待确认稿立即可读；确认同一正文只切状态，不重复快照或增加版本号。"""
    with tenant_session(temp_project) as db:
        db.add(Chapter(
            project_id=uuid.UUID(temp_project), chapter_seq=1,
            status="draft", content=None, version=1,
        ))
        db.flush()
        draft = save_review_draft(
            db, project_id=uuid.UUID(temp_project), chapter_seq=1,
            content="刚生成的正文", summary="待确认摘要",
        )
        db.flush()
        chapter_id = draft.id
        assert draft.status == "awaiting_review" and draft.content == "刚生成的正文"

    with tenant_session(temp_project) as db:
        confirmed = save_chapter(
            db, project_id=uuid.UUID(temp_project), chapter_seq=1,
            content="刚生成的正文", summary="最终摘要", generation_source="auto",
        )
        db.flush()
        assert confirmed.status == "confirmed"
        assert confirmed.version == 1
        assert db.query(ChapterVersion).filter(ChapterVersion.chapter_id == chapter_id).count() == 0


def test_put_content_snapshots_and_increments(temp_project):
    """PUT content（编辑保存）→ 快照旧正文 + 版本 +1，列表按版本降序返回。"""
    cid = _seed(temp_project)
    for i, body in enumerate(("改标点后正文", "再改一次正文"), start=2):
        resp = update_chapter_content(temp_project, cid, ContentUpdate(content=body, expected_version=i - 1))
        assert resp["version"] == i
    result = list_chapter_versions(temp_project, cid)
    assert result["current_version"] == 3
    versions = result["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["content"] == "改标点后正文"
    assert versions[1]["content"] == "v1 正文"


def test_restore_rolls_back_content_and_leaves_revert_trace(temp_project):
    """回退到历史版本 → 正文/标题覆盖回目标版，当前再快照（reason=revert），版本 +1。"""
    cid = _seed(temp_project)
    update_chapter_content(temp_project, cid, ContentUpdate(content="v2 正文", expected_version=1))
    update_chapter_content(temp_project, cid, ContentUpdate(content="v3 正文", expected_version=2))

    resp = restore_chapter_version(temp_project, cid, 2)
    assert resp["version"] == 4
    with tenant_session(temp_project) as db:
        ch = db.get(Chapter, uuid.UUID(cid))
        assert ch.content == "v2 正文" and ch.version == 4
        top = (db.query(ChapterVersion).filter(ChapterVersion.chapter_id == ch.id)
               .order_by(ChapterVersion.version.desc()).first())
        assert top.version == 3 and top.content == "v3 正文" and top.reason == "revert"


def test_versions_missing_chapter_or_version_404(temp_project):
    with pytest.raises(HTTPException) as e1:
        list_chapter_versions(temp_project, str(uuid.uuid4()))
    assert e1.value.status_code == 404

    cid = _seed(temp_project)
    with pytest.raises(HTTPException) as e2:
        restore_chapter_version(temp_project, cid, 99)
    assert e2.value.status_code == 404


def test_version_duplicate_insert_rejected_by_constraint(temp_project):
    """同章同版本号重复行被唯一约束拒（评审 M2 兜底）：并发写即使各自读到同一 version
    快照，也不落重复历史行——正常路径已靠行锁串行化，此约束是最终防线。"""
    cid = _seed(temp_project)
    with tenant_session(temp_project) as db:
        ch = db.get(Chapter, uuid.UUID(cid))
        db.add(ChapterVersion(project_id=ch.project_id, chapter_id=ch.id,
                              version=1, content="dup", reason="edit"))
        db.add(ChapterVersion(project_id=ch.project_id, chapter_id=ch.id,
                              version=1, content="dup2", reason="edit"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()  # 清掉失败 flush 状态，避免上下文管理器成功后再次 commit 抛 PendingRollbackError


def test_delete_chapter_cascades_versions(temp_project):
    """级联删章 → 版本历史随章节 FK 级联清除（不回留下孤儿历史）。"""
    cid = _seed(temp_project)
    update_chapter_content(temp_project, cid, ContentUpdate(content="v2", expected_version=1))
    from myink.api.routes_chapters import delete_chapter
    delete_chapter(temp_project, cid)
    with tenant_session(temp_project) as db:
        assert db.query(ChapterVersion).filter(
            ChapterVersion.chapter_id == uuid.UUID(cid)).count() == 0


def test_stale_editor_cannot_overwrite_newer_content(temp_project):
    cid = _seed(temp_project)
    update_chapter_content(temp_project, cid, ContentUpdate(content="另一页面已保存", expected_version=1))
    with pytest.raises(HTTPException) as exc:
        update_chapter_content(temp_project, cid, ContentUpdate(content="旧页面草稿", expected_version=1))
    assert exc.value.status_code == 409
    with tenant_session(temp_project) as db:
        ch = db.get(Chapter, uuid.UUID(cid))
        assert ch.content == "另一页面已保存" and ch.version == 2
        assert db.query(ChapterVersion).filter(ChapterVersion.chapter_id == ch.id).count() == 1


def test_two_simultaneous_saves_only_one_wins(temp_project):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    cid = _seed(temp_project)
    gate = Barrier(2)

    def save(text):
        gate.wait(timeout=10)
        try:
            update_chapter_content(temp_project, cid, ContentUpdate(content=text, expected_version=1))
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["甲页面", "乙页面"])) == [200, 409]
    with tenant_session(temp_project) as db:
        ch = db.get(Chapter, uuid.UUID(cid))
        assert ch.version == 2 and ch.content in ("甲页面", "乙页面")
