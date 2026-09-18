"""分层 collection / 事件混合召回（§7.2/§16，阶段 3 前置项）。

覆盖：
- 写侧分层：persist 软事实 → embeddings level='world'、事件 → level='event'、硬事实不索引；
- `_rrf_fuse` 纯函数（重叠排序 / 空腿 / 双命中权重 / k 值）；
- 事件混合召回：向量腿 + 关键词腿 RRF 融合，`recalled_by` 三态标注；
- 降级：embedder 抛错 → 关键词腿独立工作；双腿全空 → 纯关系兜底 + 各腿状态如实上报；
- recall_stats 六字段 + 各腿状态（vector_status/keyword_status/degrade_reason）。

**确定性 fake（关键）**：pgvector 0.8.6 实测 `cosine_distance(零向量, 任意)=NaN`——全零
fake 下 top_k 排序任意。本文件用 3-gram 特征哈希 → 1024 维单位向量（共享子串 → 高余弦
相似度 → 向量腿顺序可语义断言；避开零向量与全等向量）。模块级 autouse 覆盖 conftest
零向量版，不影响 test_flow 自己的 FakeEmbedder。
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid

import pytest
from sqlalchemy import text

from aiink.config import settings
from aiink.db import tenant_session
from aiink.models import Chapter, Event, Fact
from aiink.memory.recall import build_context, _rrf_fuse
from aiink.memory.vector_store import PgvectorStore
from aiink.workflow import nodes

QUERY_SUMMARY = "林砚在黑市查探玉佩真相"


class DeterministicFakeEmbedder:
    """确定性假 bge-m3：3-gram 特征哈希 → 1024 维单位向量。"""

    def _feat(self, text: str) -> list[float]:
        v = [0.0] * 1024
        for i in range(len(text) - 2):
            ng = text[i:i + 3]
            h = int(hashlib.sha256(ng.encode("utf-8")).hexdigest()[:8], 16)
            v[h % 1024] += 1.0
        n = (sum(x * x for x in v) ** 0.5) or 1.0
        return [x / n for x in v]

    def encode(self, texts):
        return [self._feat(t) for t in texts]


class RaiseEmbedder:
    """encode 必抛错的 fake：测向量腿独立降级。"""

    def encode(self, texts):
        raise RuntimeError("embedding 加载失败")


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    fake = DeterministicFakeEmbedder()
    monkeypatch.setattr("aiink.workflow.nodes.get_embedder", lambda: fake)
    monkeypatch.setattr("aiink.memory.recall.get_embedder", lambda: fake)
    return fake


@pytest.fixture
def embed_flag(monkeypatch):
    """显式钉死向量开关。settings 是 frozen 单例，只能替换模块里的引用（不改单例本身）；
    其取值随环境漂移（compose/ci-local=0，裸跑 pytest 取代码默认 1），凡断言腿状态的
    用例都必须自己设定，否则会随环境红绿。"""
    def _set(enabled: bool):
        monkeypatch.setattr("aiink.memory.recall.settings",
                            dataclasses.replace(settings, embed_enabled=enabled))
    return _set


def _pid() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---- 1. _rrf_fuse 纯函数 ----

def test_rrf_fuse_pure_overlap_order():
    A, B, C, D = (uuid.uuid4() for _ in range(4))
    fused = _rrf_fuse([[A, B, C], [B, C, D]])
    order = [sid for sid, _ in fused]
    assert order == [B, C, A, D], fused  # B 双命中领先
    scores = dict(fused)
    assert scores[B] == 1 / 62 + 1 / 61
    assert scores[C] == 1 / 63 + 1 / 62
    assert scores[A] == 1 / 61


def test_rrf_fuse_pure_empty_leg():
    X = uuid.uuid4()
    fused = _rrf_fuse([[], [X]])
    assert fused == [(X, 1 / 61)]


def test_rrf_fuse_pure_dual_hit_beats_single():
    X, Y = uuid.uuid4(), uuid.uuid4()
    single = dict(_rrf_fuse([[X]]))
    dual = dict(_rrf_fuse([[X], [X, Y]]))
    assert dual[X] > single[X]
    assert dual[Y] < dual[X]


def test_rrf_fuse_pure_k_relative_order_stable():
    A, B = uuid.uuid4(), uuid.uuid4()
    k1 = [sid for sid, _ in _rrf_fuse([[A, B], [B]], k=1)]
    k60 = [sid for sid, _ in _rrf_fuse([[A, B], [B]], k=60)]
    assert k1 == k60 == [B, A]


# ---- 2. 写侧分层 ----

def test_persist_indexes_world_event_levels(temp_project):
    fact_cand = {"kind": "fact", "source_chapter": 1, "confidence": 0.9,
                 "payload": {"content": "天衡宗以剑道立宗，戒律森严", "category": "势力",
                             "is_hard": False}}
    hard_cand = {"kind": "fact", "source_chapter": 1, "confidence": 0.9,
                 "payload": {"content": "林砚不得越级动用元婴秘法", "category": "规则",
                             "is_hard": True}}
    event_cand = {"kind": "event", "source_chapter": 1, "confidence": 0.9,
                  "payload": {"summary": "林砚在黑市查探玉佩真相", "participants": []}}
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 1, [fact_cand, hard_cand, event_cand])
        db.flush()
        world = db.execute(text(
            "SELECT count(*) FROM embeddings WHERE project_id=:p AND level='world'"),
            {"p": temp_project}).scalar()
        event = db.execute(text(
            "SELECT count(*) FROM embeddings WHERE project_id=:p AND level='event'"),
            {"p": temp_project}).scalar()
        hard_fact = db.execute(text(
            "SELECT count(*) FROM embeddings e JOIN facts f ON e.source_id=f.id "
            "WHERE e.project_id=:p AND f.is_hard=TRUE"), {"p": temp_project}).scalar()
    assert world == 1, "软事实应落 world 层向量"
    assert event == 1, "事件应落 event 层向量"
    assert hard_fact == 0, "硬约束恒在 Top-K、不参与相似度截断 → 不向量化"


# ---- 3. 事件混合召回（集成） ----

def _seed_hybrid_data(pid: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """seed prev 章 + 10 条无关近期事件占满 recent + 旧事件（E_both/E_vec/E_kw + 3 decoy）。

    只对旧事件 upsert embeddings（近期事件无向量 → 不参与向量腿）。
    返回 (E_both, E_vec, E_kw) 的 id。
    """
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        db.add(Chapter(project_id=p, chapter_seq=20, title="第20章",
                       content="", summary=QUERY_SUMMARY, status="confirmed"))
        # 10 条无关近期事件（ch11-20）占满 recent-10：不含"林砚"、无查询 3-gram
        for i in range(10):
            db.add(Event(project_id=p, summary=f"无关集市事件记录第{i}号",
                         participants=[], source_chapter=11 + i, confidence=0.9))
        old = [
            ("林砚在黑市查探玉佩真相", 5),   # E_both：共享 9 个 3-gram + 含"林砚"
            ("玉佩真相指向黑市，另有隐情", 6),  # E_vec：共享 玉佩真/佩真相，无"林砚"
            ("林砚独自修炼剑法", 7),        # E_kw：无共享 3-gram，含"林砚"
            ("黑市查探需谨慎", 8),          # decoy：共享 黑市查
            ("玉佩真伪难辨", 9),            # decoy：共享 玉佩真
            ("探玉佩者众多", 10),           # decoy：共享 探玉佩
        ]
        for summary, ch in old:
            db.add(Event(project_id=p, summary=summary, participants=[],
                         source_chapter=ch, confidence=0.9))
        db.flush()
        rows = db.execute(text(
            "SELECT id, summary FROM events WHERE project_id=:p AND source_chapter BETWEEN 5 AND 10"),
            {"p": pid}).fetchall()
        by_summary = {r[1]: r[0] for r in rows}  # id 已由 pg 返回为 UUID 对象
        for summary, _ in old:
            eid = by_summary[summary]
            emb = DeterministicFakeEmbedder()._feat(summary)
            PgvectorStore().upsert(db, project_id=p, level="event", source_id=eid,
                                   source_chapter=next(ch for s, ch in old if s == summary),
                                   model_version="bge-m3", embedding=emb)
        db.flush()
        return (by_summary["林砚在黑市查探玉佩真相"],
                by_summary["玉佩真相指向黑市，另有隐情"],
                by_summary["林砚独自修炼剑法"])


def test_hybrid_recall_event_tags_and_order(temp_project):
    e_both, e_vec, e_kw = _seed_hybrid_data(temp_project)
    with tenant_session(temp_project) as db:
        ctx = build_context(db, project_id=uuid.UUID(temp_project), chapter_seq=21,
                            participants=["林砚"])
    events = ctx.mid_term_events
    # 10 recent + ≤5 混合新增
    assert len(events) == 15, [e.get("summary") for e in events]
    # E_both 向量第 1 + 关键词命中 → 双命中，应在新增事件最前
    new = [e for e in events if e.get("recalled_by")]
    assert new[0]["event_id"] == str(e_both), new
    assert new[0]["recalled_by"] == "keyword+vector", new[0]["recalled_by"]
    # E_vec 纯向量；E_kw 纯关键词（被 decoy 挤出向量 top-5）
    by_id = {e["event_id"]: e for e in new}
    assert "vector" in by_id[str(e_vec)]["recalled_by"]
    assert "keyword" not in by_id[str(e_vec)]["recalled_by"]
    assert by_id[str(e_kw)]["recalled_by"] == "keyword"


# ---- 4. 降级 ----

def test_hybrid_recall_degrade_embedder_fails(temp_project, embed_flag, monkeypatch):
    _seed_hybrid_data(temp_project)
    embed_flag(True)
    with tenant_session(temp_project) as db:
        # 只 monkeypatch recall 侧 get_embedder：embedder 加载失败 → 向量腿挂，关键词腿仍工作
        monkeypatch.setattr("aiink.memory.recall.get_embedder", lambda: RaiseEmbedder())
        ctx = build_context(db, project_id=uuid.UUID(temp_project), chapter_seq=21,
                            participants=["林砚"])
        new = [e for e in ctx.mid_term_events if e.get("recalled_by")]
        assert new, "关键词腿应独立工作"
        assert all("keyword" in e["recalled_by"] for e in new), new
        assert ctx.recall_stats["keyword_hits"] >= 1
        assert ctx.recall_stats["vector_hits"] == 0
        # 开关是开的却抛错 → 这是真故障，不是「已关闭」
        assert ctx.recall_stats["vector_status"] == "failed"
        assert ctx.recall_stats["degrade_reason"] == "vector_failed"


def test_hybrid_recall_reports_vector_disabled(temp_project, embed_flag, monkeypatch):
    """默认配置（EMBED_ENABLED=0）下向量腿是「关闭」而非「故障」：状态记 disabled，
    日志走 info 不刷 warning，且关键词腿照常独立工作。"""
    _seed_hybrid_data(temp_project)
    embed_flag(False)
    monkeypatch.setattr("aiink.memory.recall.get_embedder", lambda: RaiseEmbedder())
    with tenant_session(temp_project) as db:
        ctx = build_context(db, project_id=uuid.UUID(temp_project), chapter_seq=21,
                            participants=["林砚"])
    assert ctx.recall_stats["vector_status"] == "disabled"
    assert ctx.recall_stats["vector_hits"] == 0
    assert ctx.recall_stats["keyword_status"] == "ok"
    assert ctx.recall_stats["degrade_reason"] == "vector_disabled"


def test_hybrid_recall_degrade_both_legs_empty(temp_project, embed_flag, monkeypatch):
    _seed_hybrid_data(temp_project)
    embed_flag(False)
    with tenant_session(temp_project) as db:
        monkeypatch.setattr("aiink.memory.recall.get_embedder", lambda: RaiseEmbedder())
        # participants=None → 关键词腿术语空；向量腿抛错 → 双腿全空 = 纯关系兜底
        ctx = build_context(db, project_id=uuid.UUID(temp_project), chapter_seq=21)
        assert not [e for e in ctx.mid_term_events if e.get("recalled_by")]
        # 双腿都没跑成也仍要如实上报各腿状态：返回 {} 就分不清「确实没有」和「通道瘫了」
        assert ctx.recall_stats["vector_status"] == "disabled"
        assert ctx.recall_stats["keyword_status"] == "no_terms"
        assert ctx.recall_stats["fused_total"] == 0
        assert ctx.recall_stats["degrade_reason"] == "vector_disabled+keyword_no_terms"


def test_embed_enabled_but_empty_flags_degraded(temp_project, embed_flag):
    """开关打开但索引为空：必须显式标记，不能表现为「一切正常只是没命中」——
    这正是最初那个「开了开关却什么都没发生」的静默空转。"""
    p = uuid.UUID(temp_project)
    embed_flag(True)
    with tenant_session(temp_project) as db:
        db.add(Chapter(project_id=p, chapter_seq=1, title="第1章",
                       content="", summary=QUERY_SUMMARY, status="confirmed"))
        db.flush()
        ctx = build_context(db, project_id=p, chapter_seq=2, participants=["林砚"])
    assert ctx.recall_stats["vector_status"] == "enabled_but_empty"
    assert ctx.recall_stats["degrade_reason"] == "vector_enabled_but_empty+keyword_empty"


# ---- 5. recall_stats ----

def test_recall_stats_skipped_no_prev(temp_project):
    """第 1 章没有前章摘要可作 query：显式记 skipped_no_prev，与「通道瘫了」区分开。"""
    with tenant_session(temp_project) as db:
        ctx = build_context(db, project_id=uuid.UUID(temp_project), chapter_seq=1)
    assert ctx.recall_stats == {"hybrid_status": "skipped_no_prev"}


def test_recall_stats_fields(temp_project):
    _seed_hybrid_data(temp_project)
    with tenant_session(temp_project) as db:
        ctx = build_context(db, project_id=uuid.UUID(temp_project), chapter_seq=21,
                            participants=["林砚"])
    keys = {"vector_hits", "keyword_hits", "fused_total", "recall_tokens_est",
            "context_tokens_est", "share", "vector_status", "keyword_status",
            "degrade_reason"}
    assert set(ctx.recall_stats) == keys, ctx.recall_stats
    assert ctx.recall_stats["fused_total"] == 5, ctx.recall_stats
    assert ctx.recall_stats["vector_hits"] >= 1 and ctx.recall_stats["keyword_hits"] >= 1
    assert 0 <= ctx.recall_stats["share"] <= 1, ctx.recall_stats
    assert ctx.recall_stats["context_tokens_est"] > 0
    # 双腿都正常 → 没有降级可报
    assert ctx.recall_stats["vector_status"] == "ok"
    assert ctx.recall_stats["keyword_status"] == "ok"
    assert ctx.recall_stats["degrade_reason"] is None
