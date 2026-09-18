"""题材包目录与本书快照编辑（建书深拷贝；根目录只读；只改当前书）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from myink.api.auth import require_owner
from myink.api.schemas import GenreCatalogItemOut, GenrePackOut
from myink.db import tenant_session
from myink.genre_catalog import (
    apply_field_edits,
    catalog_entries,
    is_managed_pack,
    public_pack,
    restore_baseline,
)
from myink.memory.repository import get_settings

router = APIRouter(prefix="/internal/v1", tags=["genre"])


def _pid(project_id: str) -> uuid.UUID:
    return uuid.UUID(project_id)


class GenrePackFieldsBody(BaseModel):
    selling_point: str | None = None
    subgenres: list[dict] | None = None
    taboos: list[str] | None = None
    pacing: str | None = None
    satisfaction: list[str] | None = None
    mechanics: list[str] | None = None
    world_hints: list[str] | None = None


def _fields_from_body(body: GenrePackFieldsBody) -> dict:
    data = body.model_dump(exclude_unset=True)
    return data


@router.get("/genre-packs", response_model=list[GenreCatalogItemOut])
def list_genre_packs() -> list[dict]:
    """根题材目录（只读）。"""
    return catalog_entries()


@router.put("/projects/{project_id}/genre-pack",
            dependencies=[Depends(require_owner)], response_model=GenrePackOut)
def put_genre_pack(project_id: str, body: GenrePackFieldsBody) -> dict:
    """只改本书字段，不能换根/辅题材。"""
    pid = _pid(project_id)
    with tenant_session(project_id) as db:
        st = get_settings(db, pid)
        if st is None or not is_managed_pack(st.genre_pack):
            raise HTTPException(status_code=400, detail="本书创建时未选题材包")
        try:
            st.genre_pack = apply_field_edits(st.genre_pack, _fields_from_body(body))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        st.version = (st.version or 1) + 1
        db.add(st)
        return public_pack(st.genre_pack)


@router.post("/projects/{project_id}/genre-pack/restore",
             dependencies=[Depends(require_owner)], response_model=GenrePackOut)
def restore_genre_pack(project_id: str) -> dict:
    """恢复建书时那份字段（同一本书的 baseline）。"""
    pid = _pid(project_id)
    with tenant_session(project_id) as db:
        st = get_settings(db, pid)
        if st is None or not is_managed_pack(st.genre_pack):
            raise HTTPException(status_code=400, detail="本书创建时未选题材包")
        try:
            st.genre_pack = restore_baseline(st.genre_pack)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        st.version = (st.version or 1) + 1
        db.add(st)
        return public_pack(st.genre_pack)
