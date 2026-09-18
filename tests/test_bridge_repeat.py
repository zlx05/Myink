"""桥段重复向量近邻判据（conflict-samples.md 样例 14 阳性 / 32 阴性对照，§8.6）。

L1 确定性 hint（不阻塞）：当前章事件候选摘要 vs 历史事件向量近邻——
cosine distance < 0.3 且历史事件章距 >= 10 → style/local hint，每章至多 1 条。
呼应豁免（样例 32 0 误报）：draft 或候选摘要含呼应标记词 → 整章/该候选跳过。

**确定性 fake（关键）**：pgvector 0.8.6 实测 cosine_distance(零向量, 任意)=NaN——全零
fake 下排序任意。本文件用 3-gram 特征哈希 → 1024 维单位向量（共享子串 → 高余弦相似度 →
距离可语义断言）。实证距离：CH5 vs CUR_IDENT=0.0、vs CUR_SIM≈0.19、vs UNREL=1.0——
阈值 0.3 居中，间隔宽裕。模块级 autouse 覆盖 conftest 零向量版。
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from myink.db import tenant_session
from myink.memory.vector_store import PgvectorStore
from myink.models import Event
from myink.schemas import MutationCandidate
from myink.validation.l1 import L1Validator
from myink.validation.service import ValidationService

REALM_ORDER = ["炼气", "筑基", "金丹", "元婴", "化神", "大乘", "渡劫"]

# 样例 14/32 数据：ch5 拍卖会打脸（历史事件），ch15 本章候选同款桥段
CH5 = "拍卖会上林砚被嘲讽亮出身份打脸"
CUR_IDENT = CH5
CUR_SIM = "拍卖会上林砚再次被嘲讽亮出身份打脸"   # dist≈0.19 < 0.3
UNREL = "林砚在黑市查探玉佩真相"                 # dist≈1.0  > 0.3


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
    """encode 必抛错：测向量不可用 → 检查静默跳过不阻塞（§6.12 加分项降级）。"""

    def encode(self, texts):
        raise RuntimeError("embedding 加载失败")


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    fake = DeterministicFakeEmbedder()
    monkeypatch.setattr("myink.validation.l1.get_embedder", lambda: fake)
    monkeypatch.setattr("myink.workflow.nodes.get_embedder", lambda: fake)
    monkeypatch.setattr("myink.memory.recall.get_embedder", lambda: fake)
    return fake


def _seed_event(pid: str, summary: str, chapter: int) -> uuid.UUID:
    """历史事件 + 向量入库（test_recall_hybrid._seed_hybrid_data 同款造数）。"""
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        ev = Event(project_id=p, summary=summary, participants=[],
                   source_chapter=chapter, confidence=0.9)
        db.add(ev)
        db.flush()
        PgvectorStore().upsert(db, project_id=p, level="event", source_id=ev.id,
                               source_chapter=chapter, model_version="bge-m3",
                               embedding=DeterministicFakeEmbedder()._feat(summary))
        db.flush()
        return ev.id


def _cand(summary: str, seq: int = 15, confidence: float = 0.9) -> MutationCandidate:
    return MutationCandidate(kind="event", source_chapter=seq,
                             payload={"summary": summary, "participants": []},
                             confidence=confidence)


def _check(pid: str, candidates, draft=None, seq: int = 15) -> list[dict]:
    with tenant_session(pid) as db:
        findings = L1Validator(REALM_ORDER).bridge_repeat_check(
            db, project_id=uuid.UUID(pid), chapter_seq=seq,
            candidates=candidates, draft=draft)
        return [f.model_dump(mode="json") for f in findings]


# ---- 样例 14（阳性）：跨章高相似事件 → hint ----

def test_sample14_bridge_repeat_detected(temp_project):
    _seed_event(temp_project, CH5, 5)
    fs = _check(temp_project, [_cand(CUR_IDENT)])
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "style"
    assert fs[0]["severity"] == "hint"
    assert fs[0]["scope"] == "local"
    assert fs[0]["source"] == "L1"
    assert "第 5 章" in fs[0]["evidence"][0]["quote"], fs[0]["evidence"]
    assert "拍卖" in fs[0]["evidence"][0]["quote"], fs[0]["evidence"]


def test_sample14_similar_not_identical_detected(temp_project):
    """dist≈0.19 < 0.3 命中：阈值非仅精确匹配。"""
    _seed_event(temp_project, CH5, 5)
    fs = _check(temp_project, [_cand(CUR_SIM)])
    assert len(fs) == 1, fs


def test_low_similarity_not_detected(temp_project):
    _seed_event(temp_project, CH5, 5)
    fs = _check(temp_project, [_cand(UNREL)])
    assert len(fs) == 0, fs


# ---- 样例 32（阴性）：呼应豁免 0 误报 ----

def test_sample32_callback_exempt(temp_project):
    """即便 dist=0.0 命中，draft 含呼应意图 → 整章豁免（0 误报关键）。"""
    _seed_event(temp_project, CH5, 5)
    draft = ("同样的拍卖厅，同样的叫价——林砚暗想：这一幕与当年何其相似。"
             "他亮出底牌，这次却非争锋，只为引蛇出洞。")
    fs = _check(temp_project, [_cand(CUR_IDENT)], draft=draft)
    assert len(fs) == 0, fs


def test_candidate_summary_marker_exempts(temp_project):
    """draft 缺失时候选摘要自带呼应词 → 跳过该候选。"""
    _seed_event(temp_project, CH5, 5)
    fs = _check(temp_project, [_cand("林砚忆起当年拍卖会打脸一幕")])
    assert len(fs) == 0, fs


# ---- 跨章间隔 ----

@pytest.mark.parametrize("hist_ch, seq, diff", [(12, 15, 3), (5, 14, 9)])
def test_within_gap_not_detected(temp_project, hist_ch, seq, diff):
    """章距 < 10：正常情节连续性，非偷懒重复 → 不检出。"""
    _seed_event(temp_project, CH5, hist_ch)
    assert _check(temp_project, [_cand(CUR_IDENT, seq=seq)], seq=seq) == []


def test_gap_boundary_detected(temp_project):
    """样例 14 ch5→ch15 diff=10 恰在边界 → 命中。"""
    _seed_event(temp_project, CH5, 5)
    fs = _check(temp_project, [_cand(CUR_IDENT)])
    assert len(fs) == 1, fs


def test_self_current_chapter_excluded(temp_project):
    """模拟 persist 后重跑：本章事件已入库 → gap 过滤天然排除自指。"""
    _seed_event(temp_project, CUR_IDENT, 15)
    fs = _check(temp_project, [_cand(CUR_IDENT)])
    assert len(fs) == 0, fs


# ---- 预算与降级 ----

def test_multiple_candidates_at_most_one_finding(temp_project):
    """两条高相似候选 → 每章至多 1 条（append 后即出即止）。"""
    _seed_event(temp_project, CH5, 5)
    fs = _check(temp_project, [_cand(CUR_IDENT), _cand(CUR_SIM)])
    assert len(fs) == 1, fs


def test_no_event_candidates_skip(temp_project):
    _seed_event(temp_project, CH5, 5)
    assert _check(temp_project, []) == []


def test_no_vector_data_skip(temp_project):
    """有事件无向量（embedding 缺行）→ search 返回 [] → 跳过不报。"""
    with tenant_session(temp_project) as db:
        db.add(Event(project_id=uuid.UUID(temp_project), summary=CH5, participants=[],
                     source_chapter=5, confidence=0.9))
        db.flush()
    assert _check(temp_project, [_cand(CUR_IDENT)]) == []


def test_embedder_raises_skips(temp_project, monkeypatch):
    """get_embedder 抛错 → 整方法降级，静默跳过不阻塞。"""
    _seed_event(temp_project, CH5, 5)
    monkeypatch.setattr("myink.validation.l1.get_embedder", lambda: RaiseEmbedder())
    assert _check(temp_project, [_cand(CUR_IDENT)]) == []


def test_service_wiring_appends_finding(temp_project):
    """经 ValidationService.validate 编排 → 桥段 hint 进报告（plan/target_words=None 零干扰）。"""
    _seed_event(temp_project, CH5, 5)
    with tenant_session(temp_project) as db:
        report = ValidationService(REALM_ORDER).validate(
            db, project_id=uuid.UUID(temp_project), chapter_seq=15,
            candidates=[_cand(CUR_IDENT)], draft="")
    bridge = [f for f in report.findings if f.conflict_type == "style"]
    assert len(bridge) == 1, report.findings
    assert bridge[0].severity == "hint" and bridge[0].scope == "local"
