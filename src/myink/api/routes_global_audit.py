"""全局审计手动触发（阶段 3 长线治理 · 切片 1：人设漂移抽样 L2）。

同步端点：显式动作，不做 K 门槛——窗口 = 全部未审计章（[上次审计后 +1, 当前最大章]）。
单次 LLM 调用（~30s，同 CorrectMemory 超时 caveat：同步请求请放大客户端超时）。

数据流边界 §6.2：LLM 只产出候选 findings，落库经编排层 record_report；审计失败
（LLM/解析）返回 502 且已写 status=failed 报告行（非阻塞，marker 已推进，§8.6）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from myink.api.auth import require_owner
from myink.api.schemas import AuditRunOut, GlobalAuditDetailOut, GlobalAuditSummaryOut
from myink.db import tenant_session
from myink.models import GlobalAuditReport
from myink.validation import global_audit as ga

router = APIRouter(prefix="/internal/v1", tags=["global-audit"])

# 报告列表页大小上限（前端翻页由阶段 4 前省略，只取最新一页）
_MAX_REPORTS = 20


def _project_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"项目 id 非法: {raw}") from exc


def _report_summary(r: GlobalAuditReport) -> dict:
    """列表项（不含 findings 明细，省流量）：窗口/状态/触发源/进度 marker/计数/时间。"""
    return {
        "report_id": str(r.id),
        "window_start": r.window_start,
        "window_end": r.window_end,
        "audited_up_to_chapter": r.audited_up_to_chapter,
        "trigger": r.trigger,
        "status": r.status,
        "sampled": r.summary.get("sampled", 0),
        "findings": r.summary.get("findings", 0),
        "chapters": r.summary.get("chapters", 0),
        "bridge": r.summary.get("bridge"),
        "style": r.summary.get("style"),
        "volume": r.summary.get("volume"),
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _report_detail(r: GlobalAuditReport) -> dict:
    """详情：列表项 + 抽样角色 + findings 明细（前端审计视图与章节 finding 同构渲染）。"""
    return {**_report_summary(r),
            "sampled_characters": r.sampled_characters,
            "findings": r.findings,
            "summary": r.summary}


@router.post("/projects/{project_id}/global-audit",
             dependencies=[Depends(require_owner)], response_model=AuditRunOut)
def trigger_global_audit(project_id: str) -> dict:
    """手动触发全局审计：全部未审计章窗口（不做 K 门槛），返回审计报告。

    无已写章节 → 400；LLM/解析失败 → 502（报告已落库为 failed 并推进 marker）。
    """
    pid = _project_id(project_id)
    with tenant_session(project_id) as db:
        end = ga.current_max_chapter(db, str(pid))
        if end == 0:
            raise HTTPException(status_code=400, detail="该书尚无已写章节，无法审计")
        start = ga.last_audited_up_to(db, str(pid)) + 1
        report = ga.run_global_audit(db, str(pid), (start, end), source="manual")
    if report.get("status") == "failed":
        raise HTTPException(status_code=502, detail=f"全局审计失败: {report.get('error')}")
    return report


@router.get("/projects/{project_id}/global-audit",
            dependencies=[Depends(require_owner)], response_model=list[GlobalAuditSummaryOut])
def list_global_audits(project_id: str, limit: int = _MAX_REPORTS) -> list[dict]:
    """审计报告列表（最新在前，limit ≤ 20）：窗口/状态/计数，前端审计视图导航。"""
    limit = min(max(int(limit), 1), _MAX_REPORTS)
    with tenant_session(project_id) as db:
        rows = (db.query(GlobalAuditReport)
                .order_by(GlobalAuditReport.created_at.desc(), GlobalAuditReport.id.desc())
                .limit(limit).all())
        return [_report_summary(r) for r in rows]


@router.get("/projects/{project_id}/global-audit/{report_id}",
            dependencies=[Depends(require_owner)], response_model=GlobalAuditDetailOut)
def get_global_audit(project_id: str, report_id: str) -> dict:
    """单报告详情：findings 明细 + 对照的卷/阶段 + 计数。"""
    with tenant_session(project_id) as db:
        r = db.get(GlobalAuditReport, _project_id(report_id))
        if r is None:
            raise HTTPException(status_code=404, detail="审计报告不存在")
        return _report_detail(r)
