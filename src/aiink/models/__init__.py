"""模型包：导入全部模型以注册到 Base.metadata（alembic autogenerate 依赖）。"""

from aiink.models.base import Base
from aiink.models.chapter import (
    Chapter,
    ChapterVersion,
    VolumeOutline,
)
from aiink.models.memory import (
    CHARACTER_STATE_FIELDS,
    CANDIDATE_KINDS,
    CANDIDATE_STATUSES,
    FACT_CONFIRM_STATUSES,
    FORESHADOW_STATUSES,
    GLOBAL_AUDIT_STATUSES,
    GLOBAL_AUDIT_TRIGGERS,
    LESSON_TYPES,
    PLOT_THREAD_KINDS,
    PLOT_THREAD_STATUSES,
    RELATION_TYPES,
    WRITING_LESSON_STATUSES,
    Alias,
    CharacterState,
    EmbeddingRow,
    Entity,
    Event,
    Fact,
    Foreshadow,
    GlobalAuditReport,
    MemoryCandidate,
    PlotThread,
    Relation,
    WritingLesson,
)
from aiink.models.project import (
    Character,
    Conversation,
    Faction,
    Location,
    Message,
    Project,
    ProjectSettings,
    User,
)
from aiink.models.runs import AgentRun, Task
from aiink.models.validation import Finding, ValidationReport

__all__ = [
    "Base",
    # project
    "User",
    "Project",
    "ProjectSettings",
    "Character",
    "Faction",
    "Location",
    "Conversation",
    "Message",
    # memory / graph
    "CharacterState",
    "Fact",
    "Event",
    "Relation",
    "Entity",
    "Alias",
    "Foreshadow",
    "PlotThread",
    "MemoryCandidate",
    "EmbeddingRow",
    "WritingLesson",
    "GlobalAuditReport",
    # chapter / outline
    "Chapter",
    "ChapterVersion",
    "VolumeOutline",
    # validation
    "ValidationReport",
    "Finding",
    # runs
    "Task",
    "AgentRun",
    # 枚举常量
    "CHARACTER_STATE_FIELDS",
    "RELATION_TYPES",
    "FORESHADOW_STATUSES",
    "PLOT_THREAD_KINDS",
    "PLOT_THREAD_STATUSES",
    "FACT_CONFIRM_STATUSES",
    "CANDIDATE_KINDS",
    "CANDIDATE_STATUSES",
    "WRITING_LESSON_STATUSES",
    "LESSON_TYPES",
    "GLOBAL_AUDIT_STATUSES",
    "GLOBAL_AUDIT_TRIGGERS",
]
