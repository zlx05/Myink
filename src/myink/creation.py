"""Explicit, recoverable book creation lifecycle; proposals are never canon."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from myink.db import new_session
from myink.models import Project

DRAFT_STATES = {"draft", "setup_confirmed"}


def project_payload(project: Project) -> dict:
    return {"id": str(project.id), "title": project.title, "genre": project.genre,
            "current_chapter": project.current_chapter, "target_words": project.target_words,
            "creation_status": project.creation_status}


def save_proposal(project_id: str, patch: dict) -> None:
    # Serialize concurrent setup/outline responses so neither overwrites the other.
    with new_session() as db:
        project = db.scalar(select(Project).where(Project.id == uuid.UUID(project_id)).with_for_update())
        if project is None or project.creation_status not in DRAFT_STATES:
            return
        if project.creation_status == "setup_confirmed":
            patch = {key: value for key, value in patch.items() if key != "setup_draft"}
        project.creation_context = {**(project.creation_context or {}), **patch}
        db.commit()


def validate_creation_outline(payload: dict) -> None:
    """Require meaningful goals and contiguous volume coverage before first write."""
    count = payload.get("chapter_count", 0)
    volumes = payload.get("volumes", [])
    if not str(payload.get("objective", "")).strip() or not volumes or not 50 <= count <= 1000:
        raise HTTPException(status_code=400, detail="OUTLINE_INCOMPLETE")
    next_chapter = 1
    for volume in volumes:
        start, end = volume.get("chapter_start", 0), volume.get("chapter_end", 0)
        if not volume.get("goal", "").strip() or start != next_chapter or end < start or end > count:
            raise HTTPException(status_code=400, detail="OUTLINE_INCOMPLETE")
        next_chapter = end + 1
    if next_chapter != count + 1:
        raise HTTPException(status_code=400, detail="OUTLINE_INCOMPLETE")
