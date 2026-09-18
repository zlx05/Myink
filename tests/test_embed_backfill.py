"""向量回填命令 + upsert 幂等（审计报告第 2 项的缺失拼图）。

EMBED_ENABLED 打开后若索引为空，召回静默退化为纯关键词（recall_stats 报
enabled_but_empty）；本命令负责把历史事件/事实补建成向量。核心断言是**重跑不翻倍**
——embeddings 没有 (project_id, level, source_id) 唯一约束，upsert 必须先删后插。
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
import typer
from sqlalchemy import func, select

from aiink.cli import embed_backfill
from aiink.config import settings
from aiink.db import tenant_session
from aiink.memory.vector_store import PgvectorStore
from aiink.models import EmbeddingRow, Event, Fact


@pytest.fixture
def embed_on(monkeypatch):
    """cli.embed_backfill 用延迟导入读 settings，替换模块属性即可生效。

    settings 是 frozen 单例 → 用 dataclasses.replace 造副本，不动单例本身。
    """
    def _set(enabled: bool):
        monkeypatch.setattr("aiink.config.settings",
                            dataclasses.replace(settings, embed_enabled=enabled))
    return _set


def _seed(pid: str) -> dict[str, uuid.UUID]:
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        ev = Event(project_id=p, summary="林砚在黑市查探玉佩真相", participants=[],
                   source_chapter=1, confidence=0.9)
        empty_ev = Event(project_id=p, summary="", participants=[],
                         source_chapter=2, confidence=0.9)
        soft = Fact(project_id=p, content="天衡宗以剑道立宗，戒律森严", category="势力",
                    is_hard=False, source_chapter=1, confidence=0.9, confirm_status="confirmed")
        hard = Fact(project_id=p, content="林砚不得越级动用元婴秘法", category="规则",
                    is_hard=True, source_chapter=1, confidence=0.9, confirm_status="confirmed")
        db.add_all([ev, empty_ev, soft, hard])
        db.flush()
        return {"ev": ev.id, "empty_ev": empty_ev.id, "soft": soft.id, "hard": hard.id}


def _rows(pid: str, level: str) -> list[tuple[uuid.UUID, int | None]]:
    with tenant_session(pid) as db:
        return [tuple(r) for r in db.execute(
            select(EmbeddingRow.source_id, EmbeddingRow.source_chapter).where(
                EmbeddingRow.project_id == uuid.UUID(pid),
                EmbeddingRow.level == level)).all()]


def _count(pid: str, level: str) -> int:
    with tenant_session(pid) as db:
        return db.execute(select(func.count()).select_from(EmbeddingRow).where(
            EmbeddingRow.project_id == uuid.UUID(pid), EmbeddingRow.level == level)).scalar() or 0


def test_pgvector_upsert_is_idempotent(temp_project):
    """同键重复 upsert 只留一行（先删后插）；否则同一事件在向量腿里被重复计数。"""
    pid = uuid.UUID(temp_project)
    sid = uuid.uuid4()
    with tenant_session(temp_project) as db:
        store = PgvectorStore()
        for _ in range(3):
            store.upsert(db, project_id=pid, level="event", source_id=sid, source_chapter=1,
                         model_version="bge-m3", embedding=[0.0] * 1024)
            db.flush()
    assert _rows(temp_project, "event") == [(sid, 1)]


def test_embed_backfill_event_is_idempotent(temp_project, embed_on):
    embed_on(True)
    ids = _seed(temp_project)
    embed_backfill(project=temp_project, level="event")
    first = _count(temp_project, "event")
    embed_backfill(project=temp_project, level="event")
    assert _count(temp_project, "event") == first
    assert _rows(temp_project, "event") == [(ids["ev"], 1)], "空摘要事件不索引"


def test_embed_backfill_world_skips_hard_facts(temp_project, embed_on):
    """硬约束恒在 Top-K、不参与相似度截断（§7.2）→ 向量化是浪费。"""
    embed_on(True)
    ids = _seed(temp_project)
    embed_backfill(project=temp_project, level="world")
    assert _rows(temp_project, "world") == [(ids["soft"], 1)]


def test_embed_backfill_refuses_when_embedding_disabled(temp_project, embed_on):
    """关闭时直接报错退出，而不是打印一个什么都没建成的成功数字。"""
    embed_on(False)
    _seed(temp_project)
    with pytest.raises(typer.Exit):
        embed_backfill(project=temp_project, level="event")
    assert _count(temp_project, "event") == 0


def test_embed_backfill_rejects_unknown_level(temp_project, embed_on):
    embed_on(True)
    with pytest.raises(typer.Exit):
        embed_backfill(project=temp_project, level="chapter")
