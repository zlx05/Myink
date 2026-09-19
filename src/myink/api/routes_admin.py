"""Authenticated cross-user reporting only; business RLS policies are unchanged.

Currently uses ADMIN_DATABASE_URL with a read-only transaction. Production must
provide that connection; a dedicated BYPASSRLS reporting role with SELECT-only
grants should replace the migration role in a future deployment hardening step.
Audit writes always use the ordinary application connection, separately.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import String, and_, cast, func, or_, select, text
from sqlalchemy.orm import Session

from myink.admin_observability import capture
from myink.api.admin_schemas import (
    AdminAccessLogOut, AdminChapter, AdminChapterDetail, AdminContext,
    AdminOverview, AdminPage, AdminProject, AdminRun, AdminRunDetail, AdminTask,
    AdminTaskDetail, AdminUser,
)
from myink.api.auth import _AuthenticatedUser, require_admin
from myink.db import get_admin_engine, new_session
from myink.models import (
    AdminAccessLog, AgentRun, Chapter, Character, Event, Fact, Foreshadow,
    PlotThread, Project, ProjectSettings, Task, User, VolumeOutline,
)

router = APIRouter(prefix="/internal/v1/admin", tags=["admin"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
Search = Annotated[str | None, Query(max_length=128)]
_METRIC_FIELDS = ("input_tokens", "output_tokens", "cost_est", "duration_ms")


@contextmanager
def admin_read_session():
    """Narrow reporting session; never use from owner routes or workers."""
    with Session(get_admin_engine(), autoflush=False, expire_on_commit=False) as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield db
        finally:
            db.rollback()


def write_access_log(actor_id: uuid.UUID, action: str, target: str) -> None:
    with new_session() as db:
        db.add(AdminAccessLog(actor_id=actor_id, action=action, target=target))
        db.commit()


def _safe_target(request: Request) -> str:
    # Only canonical identifiers, never query text/headers supplied by a caller.
    ids = []
    for key, raw in request.path_params.items():
        try:
            value = str(int(raw)) if key == "run_id" else str(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            value = "invalid"
        ids.append(f"{key}={value}")
    for key in ("user_id", "project_id"):
        raw = request.query_params.get(key)
        if raw:
            try:
                ids.append(f"{key}={uuid.UUID(raw)}")
            except ValueError:
                pass
    return ",".join(ids) or "collection"


def reporting_db(request: Request, actor: _AuthenticatedUser = Depends(require_admin)):
    try:
        write_access_log(actor.id, request.scope["route"].name, _safe_target(request))
    except Exception:
        raise HTTPException(503, "ADMIN_AUDIT_UNAVAILABLE") from None
    with admin_read_session() as db:
        yield db


DB = Annotated[Session, Depends(reporting_db)]


def _metric_columns(*conditions):
    def scalar(expression):
        return select(expression).where(*conditions).correlate(Project, Task, User).scalar_subquery()
    return [scalar(func.count(AgentRun.id)).label("run_count"), *[
        scalar(func.coalesce(func.sum(getattr(AgentRun, field)), 0)).label(field)
        for field in _METRIC_FIELDS
    ]]


def _task_runs(task_id, project_id):
    tid = cast(task_id, String)
    return and_(AgentRun.project_id == project_id,
                or_(AgentRun.task_id == tid, AgentRun.task_id.like(tid + ":ch%")))


def _nested_metrics(row):
    result = dict(row)
    result["metrics"] = {key: result.pop(key) for key in ("run_count", *_METRIC_FIELDS)}
    return result


def _page(db, stmt, limit, offset, transform=dict):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.execute(stmt.limit(limit).offset(offset)).mappings()
    return {"items": [transform(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def _exists(db, model, identifier):
    if db.scalar(select(model.id).where(model.id == identifier)) is None:
        raise HTTPException(404, "NOT_FOUND")


def _count(model, condition):
    return select(func.count(model.id)).where(condition).correlate(Project, User).scalar_subquery()


def _word_count(condition):
    return select(func.coalesce(func.sum(func.length(Chapter.content)), 0)).where(condition).correlate(Project, User).scalar_subquery()


@router.get("/overview", response_model=AdminOverview, name="admin.overview")
def overview(db: DB):
    counts = {name: db.scalar(select(func.count(model.id))) for name, model in (
        ("user_count", User), ("project_count", Project), ("chapter_count", Chapter), ("task_count", Task))}
    metrics = db.execute(select(*_metric_columns())).mappings().one()
    return {**counts, "metrics": dict(metrics), "task_status_counts": dict(db.execute(
        select(Task.status, func.count(Task.id)).group_by(Task.status)).all())}


@router.get("/users", response_model=AdminPage[AdminUser], name="admin.users")
def users(db: DB, q: Search = None, limit: Limit = 25, offset: Offset = 0):
    owned = select(Project.id).where(Project.user_id == User.id).correlate(User)
    stmt = select(User.id, User.username, User.tier, User.role,
                  _count(Project, Project.user_id == User.id).label("project_count"),
                  _count(Chapter, Chapter.project_id.in_(owned)).label("chapter_count"),
                  _word_count(Chapter.project_id.in_(owned)).label("word_count"),
                  _count(Task, Task.project_id.in_(owned)).label("task_count"),
                  *_metric_columns(AgentRun.project_id.in_(owned))).order_by(User.created_at.desc(), User.id.desc())
    if q:
        stmt = stmt.where(or_(User.username.icontains(q, autoescape=True), cast(User.id, String) == q))
    return _page(db, stmt, limit, offset, _nested_metrics)


@router.get("/projects", response_model=AdminPage[AdminProject], name="admin.projects")
def projects(db: DB, user_id: uuid.UUID | None = None, q: Search = None, limit: Limit = 25, offset: Offset = 0):
    stmt = select(Project.id, Project.user_id, User.username, Project.title, Project.genre,
                  Project.current_chapter, Project.target_words, Project.creation_status,
                  Project.created_at, Project.updated_at,
                  _count(Chapter, Chapter.project_id == Project.id).label("chapter_count"),
                  _word_count(Chapter.project_id == Project.id).label("word_count"),
                  _count(Task, Task.project_id == Project.id).label("task_count"),
                  *_metric_columns(AgentRun.project_id == Project.id)).join(User, User.id == Project.user_id)
    if user_id:
        stmt = stmt.where(Project.user_id == user_id)
    if q:
        stmt = stmt.where(or_(Project.title.icontains(q, autoescape=True), cast(Project.id, String) == q))
    return _page(db, stmt.order_by(Project.created_at.desc(), Project.id.desc()), limit, offset, _nested_metrics)


def _chapter_columns():
    return [Chapter.id, Chapter.project_id, Chapter.chapter_seq, Chapter.title, Chapter.status,
            func.coalesce(func.length(Chapter.content), 0).label("word_count"), Chapter.created_at, Chapter.updated_at]


@router.get("/projects/{project_id}/chapters", response_model=AdminPage[AdminChapter], name="admin.chapters")
def chapters(project_id: uuid.UUID, db: DB, limit: Limit = 25, offset: Offset = 0):
    _exists(db, Project, project_id)
    return _page(db, select(*_chapter_columns()).where(Chapter.project_id == project_id)
                 .order_by(Chapter.chapter_seq, Chapter.id), limit, offset)


@router.get("/projects/{project_id}/chapters/{chapter_id}", response_model=AdminChapterDetail, name="admin.chapter")
def chapter(project_id: uuid.UUID, chapter_id: uuid.UUID, db: DB):
    row = db.execute(select(*_chapter_columns(), Chapter.content, Chapter.summary, Chapter.version)
                     .where(Chapter.project_id == project_id, Chapter.id == chapter_id)).mappings().first()
    if row is None:
        raise HTTPException(404, "NOT_FOUND")
    return dict(row)


@router.get("/projects/{project_id}/context", response_model=AdminContext, name="admin.context")
def context(project_id: uuid.UUID, db: DB, limit: Limit = 25):
    _exists(db, Project, project_id)
    row = db.execute(select(ProjectSettings.world_rules, ProjectSettings.style_profile,
                            ProjectSettings.genre_pack, ProjectSettings.hard_constraints, ProjectSettings.version)
                     .where(ProjectSettings.project_id == project_id)).mappings().first()
    result = {"project_id": project_id, "settings": capture(dict(row) if row else None)}
    # Fixed explicit field allowlists; new schema columns never leak automatically.
    collections = (
        ("outlines", VolumeOutline, ("volume_seq", "title", "outline")),
        ("events", Event, ("summary", "participants", "source_chapter", "confidence")),
        ("facts", Fact, ("content", "category", "is_hard", "source_chapter", "confirm_status")),
        ("characters", Character, ("name", "race", "origin", "realm_cap", "personality", "base_attrs")),
        ("foreshadows", Foreshadow, ("description", "status", "planted_chapter", "resolved_chapter", "trigger")),
        ("threads", PlotThread, ("name", "kind", "status", "priority", "last_progress_chapter")),
    )
    for key, model, fields in collections:
        condition = model.project_id == project_id
        total = db.scalar(select(func.count(model.id)).where(condition))
        rows = db.execute(select(cast(model.id, String).label("id"), *[getattr(model, f) for f in fields])
                          .where(condition).order_by(model.created_at.desc(), model.id.desc()).limit(limit)).mappings()
        items = [capture(dict(item)) for item in rows]
        result[key] = {"items": items, "total": total, "limit": limit,
                       "truncated": total > limit or any(item["truncated"] for item in items)}
    return result


def _task_statement(detail=False):
    columns = [Task.id, Task.project_id, Project.user_id, User.username, Project.title.label("project_title"),
               Task.task_type, Task.status, Task.chapter_seq, Task.batch_task_id, Task.retry_count,
               Task.created_at, Task.updated_at, *_metric_columns(_task_runs(Task.id, Task.project_id))]
    if detail:
        columns += [Task.payload, Task.error]
    return select(*columns).join(Project, Project.id == Task.project_id).join(User, User.id == Project.user_id)


@router.get("/tasks", response_model=AdminPage[AdminTask], name="admin.tasks")
def tasks(db: DB, user_id: uuid.UUID | None = None, project_id: uuid.UUID | None = None,
          status: Annotated[str | None, Query(max_length=32)] = None, limit: Limit = 25, offset: Offset = 0):
    stmt = _task_statement()
    if user_id:
        stmt = stmt.where(Project.user_id == user_id)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if status:
        stmt = stmt.where(Task.status == status)
    return _page(db, stmt.order_by(Task.created_at.desc(), Task.id.desc()), limit, offset, _nested_metrics)


@router.get("/tasks/{task_id}", response_model=AdminTaskDetail, name="admin.task")
def task(task_id: uuid.UUID, db: DB):
    row = db.execute(_task_statement(True).where(Task.id == task_id)).mappings().first()
    if row is None:
        raise HTTPException(404, "NOT_FOUND")
    result = _nested_metrics(row)
    result.update(payload=capture(result["payload"]), error=capture(result["error"]),
                  elapsed_ms=max(0, int((row["updated_at"]-row["created_at"]).total_seconds()*1000)),
                  elapsed_includes_waits=True)
    return result


def _run_statement(detail=False):
    fields = ("id", "project_id", "task_id", "node", "role", "model_id", "input_tokens", "output_tokens",
              "cost_est", "duration_ms", "cache_hit", "degraded", "retry_count", "created_at", "updated_at")
    columns = [getattr(AgentRun, f) for f in fields]
    if detail:
        columns += [AgentRun.detail, AgentRun.error]
    return select(*columns, Project.user_id, User.username, Project.title.label("project_title"))\
        .join(Project, Project.id == AgentRun.project_id).join(User, User.id == Project.user_id)


@router.get("/tasks/{task_id}/runs", response_model=AdminPage[AdminRun], name="admin.task_runs")
def task_runs(task_id: uuid.UUID, db: DB, limit: Limit = 25, offset: Offset = 0):
    pid = db.scalar(select(Task.project_id).where(Task.id == task_id))
    if pid is None:
        raise HTTPException(404, "NOT_FOUND")
    return _page(db, _run_statement().where(_task_runs(str(task_id), pid))
                 .order_by(AgentRun.id), limit, offset)


@router.get("/runs", response_model=AdminPage[AdminRun], name="admin.runs")
def runs(db: DB, user_id: uuid.UUID | None = None, project_id: uuid.UUID | None = None,
         node: Annotated[str | None, Query(max_length=64)] = None, limit: Limit = 25, offset: Offset = 0):
    stmt = _run_statement()
    if user_id:
        stmt = stmt.where(Project.user_id == user_id)
    if project_id:
        stmt = stmt.where(AgentRun.project_id == project_id)
    if node:
        stmt = stmt.where(AgentRun.node == node)
    return _page(db, stmt.order_by(AgentRun.id.desc()), limit, offset)


@router.get("/runs/{run_id}", response_model=AdminRunDetail, name="admin.run")
def run(run_id: int, db: DB):
    row = db.execute(_run_statement(True).where(AgentRun.id == run_id)).mappings().first()
    if row is None:
        raise HTTPException(404, "NOT_FOUND")
    result = dict(row)
    detail = result["detail"]
    captured = capture(detail)
    previous = detail.get("_capture") if isinstance(detail, dict) else None
    if isinstance(previous, dict):
        captured["truncated"] |= bool(previous.get("truncated"))
        captured["redacted"] |= bool(previous.get("redacted"))
    result.update(detail=captured, error=capture(result["error"]), detail_missing=detail is None,
                  prompt_missing=not isinstance(detail, dict) or "messages" not in detail)
    return result


@router.get("/access-logs", response_model=AdminPage[AdminAccessLogOut], name="admin.access_logs")
def access_logs(db: DB, limit: Limit = 25, offset: Offset = 0):
    return _page(db, select(AdminAccessLog.id, AdminAccessLog.actor_id, AdminAccessLog.action,
                            AdminAccessLog.target, AdminAccessLog.created_at)
                 .order_by(AdminAccessLog.id.desc()), limit, offset)
