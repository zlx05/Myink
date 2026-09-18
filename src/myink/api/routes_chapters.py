"""章节编辑 / 记忆校正 / 版本历史 / 级联删除（阶段 3 正文轻编辑 + 阶段 4 版本表）。

- PUT content：只更新正文（版本 +1），不触发任何 LLM / 记忆动作——纯风格编辑默认零动作；
  覆盖写前把旧状态快照进 chapter_versions（版本表，历史/回退依据）；
- GET versions：章节历史版本列表（降序），含正文可供前端预览/比对；
- POST .../versions/{version}/restore：回退——快照当前（回退本身留痕）→ 覆盖回目标版本 → 版本 +1；
- POST correct-memory：显式校正——对编辑后正文重新 extract → 与该章已落库记忆 diff →
  变更集（新增/删除）进待确认池，人工 confirm/reject 后生效；
- DELETE：级联删除该章及其后全部章节（正文 + 记忆 + 版本 + 待确认池候选），进度回退到保留最大章。

数据流边界 §6.2：校正的重抽取走编排层（复用节点级 extract 路径），记忆写库仅经确认池
confirm 落库，Agent 不直写。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, func

from myink.api.auth import require_owner
from myink.api.schemas import (ChapterVersionsOut, ContentUpdateOut,
                               CorrectMemoryOut, DeleteChapterOut)
from myink.db import tenant_session
from myink.memory import correction
from myink.memory.invalidation import (invalidate_chapter_memory,
                                       purge_deleted_chapter_registry)
from myink.memory.repository import snapshot_chapter
from myink.models import Chapter, ChapterVersion, MemoryCandidate, Project
from myink.workflow import nodes

router = APIRouter(prefix="/internal/v1", tags=["chapters"])


class ContentUpdate(BaseModel):
    content: str
    expected_version: int = Field(ge=1)


def _project_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"项目 id 非法: {raw}") from exc


def _chapter_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"章节 id 非法: {raw}") from exc


@router.put("/projects/{project_id}/chapters/{chapter_id}/content",
            dependencies=[Depends(require_owner)], response_model=ContentUpdateOut)
def update_chapter_content(project_id: str, chapter_id: str, body: ContentUpdate) -> dict:
    """编辑正文（轻编辑：只写回正文 + 版本递增，不动记忆不耗 LLM）。

    用户改语言风格 / 句子长短 / 标点后直接保存；记忆校正走显式 correct-memory 端点。
    """
    with tenant_session(project_id) as db:
        # 行锁（评审 M2）：串行化同一章的并发写（用户编辑 vs 批次 persist），防版本表
        # 重复 (chapter_id, version) 行 + 丢失更新；批次侧 save_chapter 同步行锁。
        ch = db.get(Chapter, _chapter_id(chapter_id), with_for_update=True)
        if ch is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        if body.expected_version != (ch.version or 1):
            raise HTTPException(status_code=409, detail="CHAPTER_VERSION_CONFLICT")
        snapshot_chapter(db, ch)  # 覆盖写前快照进版本表（历史/回退依据，阶段 4）
        ch.content = body.content
        ch.version = (ch.version or 1) + 1
    return {"chapter_id": chapter_id, "chapter_seq": ch.chapter_seq,
            "status": ch.status, "version": ch.version}


def _version_list(db, ch: Chapter) -> list[dict]:
    versions = (db.query(ChapterVersion)
                .filter(ChapterVersion.chapter_id == ch.id)
                .order_by(ChapterVersion.version.desc()).all())
    return [{
        "version": v.version,
        "title": v.title,
        "content": v.content,
        "summary": v.summary,
        "reason": v.reason,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    } for v in versions]


@router.get("/projects/{project_id}/chapters/{chapter_id}/versions",
            dependencies=[Depends(require_owner)], response_model=ChapterVersionsOut)
def list_chapter_versions(project_id: str, chapter_id: str) -> dict:
    """章节历史版本（降序，最新在前）；含正文供前端预览/比对，不含当前实时版本。

    语义：chapters.version 是当前实时版本（不在本列表），历史 = 每次覆盖写前的快照。
    """
    with tenant_session(project_id) as db:
        ch = db.get(Chapter, _chapter_id(chapter_id))
        if ch is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        return {"chapter_id": chapter_id, "chapter_seq": ch.chapter_seq,
                "current_version": ch.version or 1, "versions": _version_list(db, ch)}


@router.post("/projects/{project_id}/chapters/{chapter_id}/versions/{version}/restore",
             dependencies=[Depends(require_owner)], response_model=ContentUpdateOut)
def restore_chapter_version(project_id: str, chapter_id: str, version: int) -> dict:
    """回退到历史版本：先快照当前（回退本身留痕为 revert）→ 覆盖正文/标题/摘要 → 版本 +1。"""
    with tenant_session(project_id) as db:
        # 行锁（评审 M2）：restore 也是覆盖写路径，与 PUT content / 批次 persist 串行化
        ch = db.get(Chapter, _chapter_id(chapter_id), with_for_update=True)
        if ch is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        target = (db.query(ChapterVersion)
                  .filter(ChapterVersion.chapter_id == ch.id,
                          ChapterVersion.version == version).one_or_none())
        if target is None:
            raise HTTPException(status_code=404, detail=f"版本 {version} 不存在")
        snapshot_chapter(db, ch, reason="revert")
        ch.content = target.content
        ch.title = target.title
        ch.summary = target.summary
        ch.version = (ch.version or 1) + 1
    return {"chapter_id": chapter_id, "chapter_seq": ch.chapter_seq,
            "status": ch.status, "version": ch.version}


@router.post("/projects/{project_id}/chapters/{chapter_id}/correct-memory",
             dependencies=[Depends(require_owner)], response_model=CorrectMemoryOut)
def correct_chapter_memory(project_id: str, chapter_id: str) -> dict:
    """显式校正记忆：编辑后正文重新抽取 → 与该章已落库记忆 diff → 变更集进待确认池。

    一次 extract LLM 调用；纯风格编辑 → no_op=True、池零新增。返回变更报告供前端展示，
    变更集的人工确认复用现有 candidates confirm/reject 端点。
    """
    with tenant_session(project_id) as db:
        ch = db.get(Chapter, _chapter_id(chapter_id))
        if ch is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        if not ch.content:
            raise HTTPException(status_code=400, detail="该章无正文，无法校正")
        candidates, err = nodes.extract_candidates_from_draft(
            db, project_id=project_id, chapter_seq=ch.chapter_seq, draft=ch.content,
            task_id=None)
        if err:
            raise HTTPException(status_code=502, detail=f"记忆抽取失败: {err}")
        report = correction.correct_chapter_memory(
            db, project_id=_project_id(project_id), chapter_seq=ch.chapter_seq,
            new_candidates=candidates)
    return report


@router.delete("/projects/{project_id}/chapters/{chapter_id}",
               dependencies=[Depends(require_owner)], response_model=DeleteChapterOut)
def delete_chapter(project_id: str, chapter_id: str) -> dict:
    """级联删除章节：删除该章及其后全部章节（正文 + 记忆 + 待确认池候选 + 登记表产物）。

    语义（阶段 3 决策）：中间章删掉后后续章剧情引用已删事件会断层，级联删除让作者
    从被删章重新生成。进度回退到保留的最大章序；无保留章则回 0。
    登记表（人物卡/设定实体/别名）按被删章节产物精准清理（purge_deleted_chapter_registry），
    建书确认的势力/地点/世界观规则保留。边界：删除前应确保该范围无进行中的生成/续跑任务。
    """
    with tenant_session(project_id) as db:
        ch = db.get(Chapter, _chapter_id(chapter_id))
        if ch is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        seq = ch.chapter_seq
        rows = (db.query(Chapter).filter(Chapter.chapter_seq >= seq)
                .order_by(Chapter.chapter_seq).all())
        # 先清登记表再失效：失效会硬删被删范围事件，而人物引用判定依赖事件参与者，
        # 必须先算（§审计整改：删章不再残留人物卡/武器实体/悬空关系边）。
        purge_deleted_chapter_registry(db, project_id=uuid.UUID(project_id), from_seq=seq)
        deleted: list[dict] = []
        for c in rows:
            invalidation = invalidate_chapter_memory(db, project_id=uuid.UUID(project_id),
                                                     chapter_seq=c.chapter_seq)
            db.execute(sa_delete(MemoryCandidate).where(
                MemoryCandidate.source_chapter == c.chapter_seq))
            deleted.append({"chapter_seq": c.chapter_seq, "title": c.title,
                            "invalidation": invalidation})
            db.delete(c)
        db.flush()  # autoflush=False：先落删除，max 才反映真实保留集
        proj = db.get(Project, uuid.UUID(project_id))
        remaining = db.query(func.max(Chapter.chapter_seq)).scalar() or 0
        if proj is not None:
            proj.current_chapter = remaining
    return {"deleted": deleted, "current_chapter": remaining}
