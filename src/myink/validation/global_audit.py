"""全局审计：对照整书卷规划，看已写章节有没有推进到位。

人设 / 文风 / 桥段由单章审核负责。这里只问：窗口内剧情是否沿着卷目标、阶段目标走；
偏了或明显落后，给出怎么拉回来。

每 K 章（默认 30，约一个阶段）批次触发，或手动审全部未审计章。无大纲则空报告推进
marker，不调 LLM。LLM 只出候选，落库走编排层；引文须落在窗口摘要/正文里。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from myink.memory.repository import get_volume_outline
from myink.models import Chapter, GlobalAuditReport
from myink.providers import make_chain
from myink.workflow.outline import covering_item, normalize_outline

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.6
MAX_PROGRESS = 40
PROGRESS_CHARS = 240


def last_audited_up_to(db: Session, project_id) -> int:
    row = (db.query(func.max(GlobalAuditReport.audited_up_to_chapter))
           .filter(GlobalAuditReport.project_id == project_id).scalar())
    return row or 0


def current_max_chapter(db: Session, project_id) -> int:
    row = (db.query(func.max(Chapter.chapter_seq))
           .filter(Chapter.project_id == project_id).scalar())
    return row or 0


def window_for_batch(db: Session, project_id, *, K: int) -> tuple[int, int] | None:
    start = last_audited_up_to(db, project_id) + 1
    end = current_max_chapter(db, project_id)
    if end - start + 1 < K:
        return None
    return (start, end)


def _even_pick(items: list, cap: int) -> list:
    if len(items) <= cap:
        return items
    if cap <= 1:
        return items[:1]
    last = len(items) - 1
    return [items[round(i * last / (cap - 1))] for i in range(cap)]


def assemble_volume_context(db: Session, project_id, window: tuple[int, int]) -> dict | None:
    """无大纲 → None。有大纲则切出与窗口重叠的卷/阶段 + 已写摘要。"""
    row = get_volume_outline(db, uuid.UUID(str(project_id)), 1)
    if row is None or not isinstance(row.outline, dict):
        return None
    outline = normalize_outline(row.outline) or {}
    volumes = [v for v in (outline.get("volumes") or []) if isinstance(v, dict)]
    if not volumes:
        return None
    lo, hi = window
    picked = []
    for v in volumes:
        stages = [s for s in (v.get("stages") or []) if isinstance(s, dict)]
        overlap_stages = [s for s in stages
                          if _as_int(s.get("chapter_start")) <= hi
                          and _as_int(s.get("chapter_end")) >= lo]
        v_lo, v_hi = _as_int(v.get("chapter_start")), _as_int(v.get("chapter_end"))
        if overlap_stages or (v_lo and v_hi and v_lo <= hi and v_hi >= lo):
            picked.append({
                "volume_seq": v.get("volume_seq"),
                "title": v.get("title") or "",
                "goal": v.get("goal") or "",
                "key_results": list(v.get("key_results") or []),
                "end_event": v.get("end_event") or "",
                "chapter_start": v_lo,
                "chapter_end": v_hi,
                "stages": overlap_stages or stages,
            })
    if not picked:
        last = covering_item(volumes, hi) or volumes[-1]
        picked = [{
            "volume_seq": last.get("volume_seq"),
            "title": last.get("title") or "",
            "goal": last.get("goal") or "",
            "key_results": list(last.get("key_results") or []),
            "end_event": last.get("end_event") or "",
            "chapter_start": last.get("chapter_start"),
            "chapter_end": last.get("chapter_end"),
            "stages": list(last.get("stages") or []),
        }]
    pid = uuid.UUID(str(project_id))
    chapters = (db.query(Chapter).filter(
        Chapter.project_id == pid,
        Chapter.chapter_seq >= lo,
        Chapter.chapter_seq <= hi)
        .order_by(Chapter.chapter_seq).all())
    progress = []
    for c in _even_pick(chapters, MAX_PROGRESS):
        text = (c.summary or "").strip() or (c.content or "")[:PROGRESS_CHARS]
        progress.append({"seq": c.chapter_seq, "text": text.strip(),
                         "content": c.content or "", "summary": c.summary or ""})
    return {
        "objective": outline.get("objective") or "",
        "volumes": picked,
        "progress": progress,
    }


def _as_int(value, default: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n


def _evidence_text(evidence) -> str | None:
    if isinstance(evidence, str):
        return evidence.strip() or None
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
        q = evidence[0].get("quote")
        return str(q).strip() if q else None
    return None


def normalize_and_verify_findings(db: Session, project_id, raw, ctx: dict,
                                  window: tuple[int, int]) -> list[dict]:
    """丢弃：verdict 非法、章越窗、置信度低、引文不在窗口正文/摘要里。"""
    haystacks = {}
    for p in ctx.get("progress") or []:
        haystacks[p["seq"]] = (p.get("summary") or "") + (p.get("content") or "") + (p.get("text") or "")
    contents = {c.chapter_seq: (c.summary or "") + (c.content or "") for c in db.query(Chapter).filter(
        Chapter.project_id == uuid.UUID(str(project_id)),
        Chapter.chapter_seq >= window[0],
        Chapter.chapter_seq <= window[1]).all()}
    verified: dict[tuple, dict] = {}
    for f in raw or []:
        if not isinstance(f, dict):
            continue
        verdict = f.get("verdict")
        chapter = f.get("chapter")
        quote = _evidence_text(f.get("evidence"))
        reason = str(f.get("reason") or "").strip()
        recovery = str(f.get("recovery") or f.get("suggestion") or "").strip()
        try:
            confidence = float(f.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if verdict not in ("drifted", "behind") or not isinstance(chapter, int):
            continue
        if chapter < window[0] or chapter > window[1] or not quote or not reason:
            continue
        if confidence < MIN_CONFIDENCE:
            continue
        blob = haystacks.get(chapter) or contents.get(chapter) or ""
        if quote not in blob:
            continue
        vol = _as_int(f.get("volume_seq")) or 1
        stage = _as_int(f.get("stage_seq")) or 1
        label = "偏离卷规划" if verdict == "drifted" else "落后于卷规划"
        key = (verdict, vol, stage)
        item = {
            "conflict_key": f"volume:{vol}:{stage}:{chapter}",
            "conflict_type": "volume",
            "severity": "hint",
            "scope": "structural",
            "source": "L2",
            "evidence": [{"chapter": chapter, "quote": quote}],
            "confidence": confidence,
            "suggestion": recovery or f"{label}：{reason}",
        }
        prev = verified.get(key)
        if prev is None or confidence > float(prev.get("confidence") or 0):
            verified[key] = item
    return list(verified.values())


def record_report(db: Session, *, project_id, window: tuple[int, int], findings: list[dict],
                  sampled: list[dict], status: str, error: str | None, source: str,
                  source_batch_task_id: str | None = None,
                  kind_errors: dict[str, str] | None = None,
                  extra_summary: dict | None = None) -> dict:
    window_start, window_end = window
    summary: dict = {"sampled": len(sampled), "findings": len(findings),
                     "chapters": window_end - window_start + 1}
    if extra_summary:
        summary.update(extra_summary)
    if kind_errors:
        summary["errors"] = dict(kind_errors)
    db.add(GlobalAuditReport(
        project_id=uuid.UUID(str(project_id)), window_start=window_start, window_end=window_end,
        audited_up_to_chapter=window_end, trigger=source,
        source_batch_task_id=source_batch_task_id, status=status,
        sampled_characters=sampled, findings=findings,
        summary=summary, error=error,
    ))
    db.flush()
    return {
        "window_start": window_start, "window_end": window_end,
        "audited_up_to_chapter": window_end, "status": status,
        "sampled_characters": sampled, "findings": findings, "error": error,
        "summary": summary,
    }


def _run_kind_llm(db: Session, project_id, task_id: str | None, window: tuple[int, int],
                  messages: list[dict], *, detail: dict) -> tuple[list | None, str | None]:
    from myink.workflow import nodes

    resp = make_chain("audit", db=db, project_id=str(project_id)).generate(
        messages, json_mode=True, max_tokens=nodes._MAX_TOKENS["audit"])
    nodes.record_run(db, project_id=project_id, task_id=task_id, node="global_audit",
                     role="GlobalAudit", resp=resp, error=resp.error, detail=detail)
    if resp.error:
        return None, resp.error
    try:
        data = nodes._parse_json(resp.content)
    except Exception as exc:  # noqa: BLE001
        return None, f"parse_error: {exc}"
    raw = data.get("findings", []) if isinstance(data, dict) else []
    return raw, None


def run_global_audit(db: Session, project_id, window: tuple[int, int], *,
                     source: str = "manual", source_batch_task_id: str | None = None) -> dict:
    """窗口内对照卷规划做一次推进审计。无大纲 / LLM 失败都不阻塞写作。"""
    from myink.workflow import prompts

    pid = str(project_id)
    ctx = assemble_volume_context(db, pid, window)
    if ctx is None:
        return record_report(
            db, project_id=pid, window=window, findings=[], sampled=[],
            status="completed", error=None, source=source,
            source_batch_task_id=source_batch_task_id,
            extra_summary={"reason": "no_outline"})

    sampled = []
    for v in ctx["volumes"]:
        for s in v.get("stages") or [{}]:
            sampled.append({
                "volume_seq": v.get("volume_seq"),
                "title": v.get("title") or "",
                "stage_seq": s.get("stage_seq"),
                "name": s.get("name") or "",
            })
    messages = prompts.global_audit_messages(ctx, window)
    raw, err = _run_kind_llm(
        db, pid, source_batch_task_id, window, messages,
        detail={"window": [window[0], window[1]],
                "volumes": [v.get("volume_seq") for v in ctx["volumes"]]})
    if err:
        logger.warning("卷推进审计失败（不阻塞，marker 已推进）: %s", err)
        return record_report(
            db, project_id=pid, window=window, findings=[], sampled=sampled,
            status="failed", error=err, source=source,
            source_batch_task_id=source_batch_task_id,
            extra_summary={"volume": {"volumes": len(ctx["volumes"]),
                                      "stages": len(sampled)}})

    findings = normalize_and_verify_findings(db, pid, raw, ctx, window)
    return record_report(
        db, project_id=pid, window=window, findings=findings, sampled=sampled,
        status="completed", error=None, source=source,
        source_batch_task_id=source_batch_task_id,
        extra_summary={"volume": {"volumes": len(ctx["volumes"]), "stages": len(sampled)}})
