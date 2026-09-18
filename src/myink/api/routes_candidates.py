"""记忆候选内部端点（§6.11 确认分流 / §7.3 事实生命周期）。

critical 冲突时 persist 把 extract 候选写入待确认池、章节/任务标 awaiting_review；
本端点提供人工**确认/拒绝**（数据流边界 §6.2：确认 = 编排层写库入口之一，
与 persist 自动放行走完全相同的落库路径 nodes.confirm_candidate）。处理完候选后
用户 resume 任务从 checkpoint 续跑放行（正文落库发生在 persist 重跑）。阶段 4
前端补批量确认 UI，此处为后端闭环。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException

from myink.api.auth import require_owner
from myink.api.schemas import CandidateActionOut, MemoryCandidateOut
from myink.db import tenant_session
from myink.models import MemoryCandidate
from myink.workflow import nodes

router = APIRouter(prefix="/internal/v1", tags=["candidates"])


class RejectCandidateIn(BaseModel):
    mode: Literal["revise", "memory_only"] = "revise"
    reason: str = Field(default="", max_length=2000)


def _cand_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"候选 id 非法: {raw}") from exc


@router.get("/projects/{project_id}/candidates",
            dependencies=[Depends(require_owner)], response_model=list[MemoryCandidateOut])
def list_candidates(project_id: str, status: str = "pending") -> list[dict]:
    """待确认池列表（按章排序，含 payload 供前端展示）。"""
    with tenant_session(project_id) as db:
        q = db.query(MemoryCandidate).order_by(MemoryCandidate.source_chapter)
        if status:
            q = q.filter(MemoryCandidate.status == status)
        return [
            {
                "candidate_id": str(c.id),
                "kind": c.kind,
                "source_chapter": c.source_chapter,
                "payload": c.payload,
                "confidence": c.confidence,
                "status": c.status,
                "review": c.review,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in q.all()
        ]


@router.post("/projects/{project_id}/candidates/{candidate_id}/confirm",
             dependencies=[Depends(require_owner)], response_model=CandidateActionOut)
def confirm_candidate(project_id: str, candidate_id: str) -> dict:
    """确认候选落库（Event/Fact/CharacterState…，§7.3）。幂等：已处理候选返回 409。"""
    with tenant_session(project_id) as db:
        cand = nodes.confirm_candidate(db, project_id, _cand_id(candidate_id))
        if cand is None:
            raise HTTPException(status_code=409, detail="候选不存在或非 pending，不可确认")
        db.commit()
    return {"candidate_id": candidate_id, "status": "confirmed"}


@router.post("/projects/{project_id}/candidates/{candidate_id}/reject",
             dependencies=[Depends(require_owner)], response_model=CandidateActionOut)
def reject_candidate(project_id: str, candidate_id: str, body: RejectCandidateIn | None = None) -> dict:
    """拒绝候选（幻觉/误抽清理）。幂等：已处理候选返回 409。"""
    with tenant_session(project_id) as db:
        cand = db.get(MemoryCandidate, _cand_id(candidate_id))
        if cand is None or str(cand.project_id) != project_id:
            raise HTTPException(status_code=404, detail="候选不存在")
        if cand.status != "pending":
            raise HTTPException(status_code=409, detail=f"候选已处理: {cand.status}")
        cand.status = "rejected"
        decision = body or RejectCandidateIn()
        cand.review = {"mode": decision.mode, "reason": decision.reason, "applied": False}
        db.commit()
    return {"candidate_id": candidate_id, "status": "rejected"}
