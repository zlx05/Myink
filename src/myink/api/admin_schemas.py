"""Explicit, GET-only reporting contracts. Narrative fields are plain text."""
from datetime import datetime
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue

T = TypeVar("T")


class AdminPage(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class CaptureLimits(BaseModel):
    max_depth: int
    max_items: int
    max_text: int
    max_bytes: int


class CapturedData(BaseModel):
    data: JsonValue
    truncated: bool
    redacted: bool
    limits: CaptureLimits


class AdminMetrics(BaseModel):
    run_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_est: float = Field(0, description="Estimated cost, not an invoice")
    duration_ms: int = Field(0, description="Sum of measured node durations; 0 can mean unmeasured")


class AdminOverview(BaseModel):
    user_count: int
    project_count: int
    chapter_count: int
    task_count: int
    metrics: AdminMetrics
    task_status_counts: dict[str, int]


class AdminUser(BaseModel):
    id: UUID
    username: str
    tier: str
    role: Literal["user", "admin"]
    project_count: int
    chapter_count: int
    word_count: int
    task_count: int
    metrics: AdminMetrics


class AdminProject(BaseModel):
    id: UUID
    user_id: UUID
    username: str
    title: str
    genre: str
    current_chapter: int
    target_words: int | None
    creation_status: str
    created_at: datetime
    updated_at: datetime
    chapter_count: int
    word_count: int
    task_count: int
    metrics: AdminMetrics


class AdminChapter(BaseModel):
    id: UUID
    project_id: UUID
    chapter_seq: int
    title: str | None
    status: str
    word_count: int
    created_at: datetime
    updated_at: datetime


class AdminChapterDetail(AdminChapter):
    content: str | None
    summary: str | None
    version: int


class AdminContextCollection(BaseModel):
    items: list[CapturedData]
    total: int
    limit: int
    truncated: bool


class AdminContext(BaseModel):
    project_id: UUID
    settings: CapturedData
    outlines: AdminContextCollection
    events: AdminContextCollection
    facts: AdminContextCollection
    characters: AdminContextCollection
    foreshadows: AdminContextCollection
    threads: AdminContextCollection


class AdminTask(BaseModel):
    id: UUID
    project_id: UUID
    user_id: UUID
    username: str
    project_title: str
    task_type: str
    status: str
    chapter_seq: int | None
    batch_task_id: UUID | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    metrics: AdminMetrics


class AdminTaskDetail(AdminTask):
    payload: CapturedData
    error: CapturedData
    elapsed_ms: int = Field(description="created_at to updated_at; includes queue/pause/human waits")
    elapsed_includes_waits: Literal[True] = True


class AdminRun(BaseModel):
    id: int
    project_id: UUID
    user_id: UUID
    username: str
    project_title: str
    task_id: str | None
    node: str
    role: str | None
    model_id: str | None
    input_tokens: int
    output_tokens: int
    cost_est: float
    duration_ms: int
    cache_hit: bool
    degraded: bool
    retry_count: int
    created_at: datetime
    updated_at: datetime


class AdminRunDetail(AdminRun):
    detail: CapturedData
    error: CapturedData
    detail_missing: bool
    prompt_missing: bool


class AdminAccessLogOut(BaseModel):
    id: int
    actor_id: UUID
    action: str
    target: str
    created_at: datetime
