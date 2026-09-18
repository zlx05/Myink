"""写作经验内部端点（§8.9 reflexion 确认分流）。

批次收尾 reflexion 把高危经验（源自 critical/major finding）写入 writing_lessons
待确认池（status=proposed）；本端点提供人工**确认/拒绝**（数据流边界 §6.2：确认 =
编排层写库入口之一，与 persist 同层）。确认后经验 status=active，随下一批
recall 注入后续章节的规划/写作（越写越懂这本书）。阶段 4 前端补批量确认 UI，
此处为后端闭环。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from myink.api.auth import require_owner
from myink.api.schemas import LessonActionOut, WritingLessonOut
from myink.db import tenant_session
from myink.models import WritingLesson
from myink.workflow import nodes

router = APIRouter(prefix="/internal/v1", tags=["lessons"])


def _lesson_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"经验 id 非法: {raw}") from exc


@router.get("/projects/{project_id}/lessons",
            dependencies=[Depends(require_owner)], response_model=list[WritingLessonOut])
def list_lessons(project_id: str, status: str | None = None) -> list[dict]:
    """写作经验列表（reflexion 复盘，按来源章排序；status 可选过滤）。"""
    with tenant_session(project_id) as db:
        q = db.query(WritingLesson).order_by(WritingLesson.source_chapter)
        if status:
            q = q.filter(WritingLesson.status == status)
        return [
            {
                "lesson_id": str(l.id),
                "category": l.category,
                "lesson_type": l.lesson_type,
                "content": l.content,
                "evidence": l.evidence,
                "confidence": l.confidence,
                "source_chapter": l.source_chapter,
                "status": l.status,
                "recurrence_count": l.recurrence_count,
                "last_recurrence_at": l.last_recurrence_at,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in q.all()
        ]


@router.post("/projects/{project_id}/lessons/{lesson_id}/confirm",
             dependencies=[Depends(require_owner)], response_model=LessonActionOut)
def confirm_lesson(project_id: str, lesson_id: str) -> dict:
    """确认经验生效（proposed→active，后续章节注入遵守）。幂等：非 proposed 返回 409。"""
    with tenant_session(project_id) as db:
        row = nodes.confirm_lesson(db, project_id, _lesson_id(lesson_id))
        if row is None:
            raise HTTPException(status_code=409, detail="经验不存在或非 proposed，不可确认")
        db.commit()
    return {"lesson_id": lesson_id, "status": "active"}


@router.post("/projects/{project_id}/lessons/{lesson_id}/reject",
             dependencies=[Depends(require_owner)], response_model=LessonActionOut)
def reject_lesson(project_id: str, lesson_id: str) -> dict:
    """拒绝经验（proposed→rejected，幻觉/无益经验清理）。幂等：非 proposed 返回 409。"""
    with tenant_session(project_id) as db:
        row = nodes.reject_lesson(db, project_id, _lesson_id(lesson_id))
        if row is None:
            raise HTTPException(status_code=409, detail="经验不存在或非 proposed，不可拒绝")
        db.commit()
    return {"lesson_id": lesson_id, "status": "rejected"}
