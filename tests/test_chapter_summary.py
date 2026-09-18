"""LLM 章节摘要测试（§7 短期记忆，node_summarize + finalize 摘要回归修复）。

mock LLM（不依赖真实 DeepSeek key）。验证：
- node_summarize：confirmed 章落库后追加 LLM 真摘要；非 confirmed 短路不产；LLM 失败
  保留启发式摘要（非阻塞，绝不拉坏主链路）；
- finalize 回归：finalize_awaiting_review 掩码后事件候选摘要不再丢（summary_candidates 键）——
  用户实测「确认后章节摘要全没了」的修复；
- runner.finalize_chapter_review 在收尾落库后追加调 node_summarize。
"""

from __future__ import annotations

import uuid

from myink.db import tenant_session
from myink.models import Chapter
from myink.providers.base import ModelResponse
from myink.workflow import nodes
from myink.workflow.runner import finalize_chapter_review


def _get_chapter(temp_project: str):
    with tenant_session(temp_project) as db:
        return db.query(Chapter).filter(
            Chapter.project_id == uuid.UUID(temp_project), Chapter.chapter_seq == 1).first()


def _make_confirmed_chapter(temp_project: str, *, summary: str | None = "启发式摘要") -> None:
    with tenant_session(temp_project) as db:
        db.add(Chapter(project_id=uuid.UUID(temp_project), chapter_seq=1, status="confirmed",
                       content="正文草稿", summary=summary))
        db.commit()


def test_node_summarize_writes_llm_summary(temp_project, stub_provider):
    """confirmed 章：node_summarize 以正文为输入产出 LLM 真摘要覆盖启发式。"""
    stub_provider("金丹", "金丹")
    _make_confirmed_chapter(temp_project)

    out = nodes.node_summarize({
        "project_id": temp_project, "chapter_seq": 1, "draft": "正文草稿",
    })
    assert out == {}
    assert _get_chapter(temp_project).summary == "本章测试摘要", "LLM 摘要应覆盖启发式"


def test_node_summarize_skips_non_confirmed(temp_project, monkeypatch):
    """非 confirmed（awaiting_review 首跑）：短路不调 LLM、不改摘要。"""
    with tenant_session(temp_project) as db:
        db.add(Chapter(project_id=uuid.UUID(temp_project), chapter_seq=1, status="awaiting_review",
                       content="正文草稿", summary=None))
        db.commit()

    def _boom(*_a, **_kw):
        raise AssertionError("非 confirmed 不应调 make_chain")

    monkeypatch.setattr(nodes, "make_chain", _boom)
    out = nodes.node_summarize({
        "project_id": temp_project, "chapter_seq": 1, "draft": "正文草稿",
    })
    assert out == {}
    assert _get_chapter(temp_project).summary is None, "非 confirmed 不应写入摘要"


def test_node_summarize_llm_failure_keeps_heuristic(temp_project, monkeypatch):
    """LLM 失败：只记 warning、保留启发式摘要，不抛异常不阻塞。"""
    _make_confirmed_chapter(temp_project)

    class _FailingChain:
        def generate(self, messages, *, json_mode=False, max_tokens=None, temperature=None, tools=None):
            return ModelResponse(content="", model_id="stub", error="摘要服务失败",
                                 input_tokens=10, output_tokens=0, duration_ms=10)

    monkeypatch.setattr(nodes, "make_chain", lambda role, **kw: _FailingChain())
    out = nodes.node_summarize({
        "project_id": temp_project, "chapter_seq": 1, "draft": "正文草稿",
    })
    assert out == {}
    assert _get_chapter(temp_project).summary == "启发式摘要", "LLM 失败应保留启发式摘要"


def test_finalize_awaiting_review_keeps_event_summary(temp_project, fake_embedder):
    """回归：finalize_awaiting_review 掩码后 candidates 只剩 plotline，事件摘要曾全丢。

    修复：完整原候选走 summary_candidates 键 → node_persist 落库时取它拼章节摘要。
    """
    task_id = str(uuid.uuid4())
    out = nodes.finalize_awaiting_review({
        "project_id": temp_project,
        "chapter_seq": 1,
        "draft": "林砚在黑市隐秘查探，玉佩气息若隐若现。",
        "candidates": [
            {"kind": "event", "confidence": 0.9,
             "payload": {"summary": "林砚于黑市查探玉佩真相", "participants": ["林砚"],
                         "source_chapter": 1, "confidence": 0.9}},
            {"kind": "plotline", "payload": {"thread_name": "主线"}},
        ],
    }, task_id=task_id)
    assert out.get("needs_review") is False

    ch = _get_chapter(temp_project)
    assert ch is not None and ch.status == "confirmed", "收尾应落库且章节 confirmed"
    assert ch.content == "林砚在黑市隐秘查探，玉佩气息若隐若现。"
    assert ch.summary and "林砚于黑市查探玉佩真相" in ch.summary, \
        f"收尾后摘要应含事件摘要，实际 {ch.summary!r}"


def test_runner_finalize_appends_node_summarize(temp_project, monkeypatch):
    """finalize 不走图 → runner 在落库后单独补调 node_summarize（含完整 checkpoint 状态）。"""
    from myink.workflow import runner as runner_mod

    class _FakeSnap:
        values = {
            "project_id": temp_project, "chapter_seq": 1, "draft": "正文草稿",
            "candidates": [{"kind": "event", "confidence": 0.9, "payload": {"summary": "s"}}],
        }

    class _FakeGraph:
        def get_state(self, config):
            return _FakeSnap()

    calls = []
    monkeypatch.setattr(runner_mod, "get_graphs", lambda: (_FakeGraph(), None))
    monkeypatch.setattr(nodes, "finalize_awaiting_review",
                        lambda values, **kw: {"needs_review": False})
    monkeypatch.setattr(nodes, "node_summarize", lambda state: calls.append(state))

    result = finalize_chapter_review(project_id=temp_project, task_id="t1", chapter_seq=1)
    assert result["needs_review"] is False
    assert len(calls) == 1, "收尾后应补调 node_summarize"
    assert calls[0]["task_id"] == "t1" and calls[0]["draft"] == "正文草稿"
