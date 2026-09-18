"""伏笔抽取驱动自动回收测试（§7.9 foreshadow_touch）。

用户实测「伏笔只埋不收」→ 决策：抽取驱动自动回收（低风险，不进候选池不阻塞）。
mock LLM 或直测节点（不依赖真实 DeepSeek key）。验证：
- _persist_candidates foreshadow_touch 分支：advanced→developing、resolved→resolved、
  resolved 不倒退、坏/不存在 id 跳过（不崩批次）；
- node_persist 两条路径：auto 放行 / awaiting_review 分流，foreshadow_touch 均自动落库、
  不进待确认池；
- extract_messages 注入开放伏笔块（foreshadow_id 的合法来源）。
"""

from __future__ import annotations

import uuid

from myink.db import tenant_session
from myink.models import Chapter, Foreshadow, MemoryCandidate
from myink.workflow import nodes, prompts


def _plant(temp_project: str, *, status: str = "planted", chapter: int = 1):
    with tenant_session(temp_project) as db:
        fs = Foreshadow(project_id=uuid.UUID(temp_project), description="玉佩之谜",
                        status=status, planted_chapter=chapter, trigger={})
        db.add(fs)
        db.flush()
        return str(fs.id)


def _touch(temp_project: str, fs_id: str, outcome: str, chapter_seq: int = 2) -> None:
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, chapter_seq, [
            {"kind": "foreshadow_touch", "confidence": 0.9,
             "payload": {"foreshadow_id": fs_id, "outcome": outcome, "note": "本章推进"}},
        ])
        db.flush()


def _fs(temp_project: str, fs_id: str):
    with tenant_session(temp_project) as db:
        fs = db.get(Foreshadow, uuid.UUID(fs_id))
        return {"status": fs.status, "last_touched": fs.last_touched,
                "resolved_chapter": fs.resolved_chapter}


def test_touch_advanced_develops(temp_project):
    fs_id = _plant(temp_project)
    _touch(temp_project, fs_id, "advanced")
    fs = _fs(temp_project, fs_id)
    assert fs == {"status": "developing", "last_touched": 2, "resolved_chapter": None}


def test_touch_resolved_sets_chapter(temp_project):
    fs_id = _plant(temp_project)
    _touch(temp_project, fs_id, "resolved")
    fs = _fs(temp_project, fs_id)
    assert fs == {"status": "resolved", "last_touched": 2, "resolved_chapter": 2}


def test_touch_resolved_not_reverted_by_advanced(temp_project):
    """已 resolved 伏笔 + advanced → 不倒退（状态/回收章不变，不再 touch）。"""
    fs_id = _plant(temp_project)
    _touch(temp_project, fs_id, "resolved", chapter_seq=2)
    _touch(temp_project, fs_id, "advanced", chapter_seq=5)
    fs = _fs(temp_project, fs_id)
    assert fs == {"status": "resolved", "last_touched": 2, "resolved_chapter": 2}, \
        "已解决伏笔不应被 advanced 倒退或改写"


def test_touch_bad_or_missing_id_skipped(temp_project):
    """坏 id / 项目内不存在的 id → 跳过（RLS + 显式 None 检查兜底防幻觉），不崩。"""
    with tenant_session(temp_project) as db:
        nodes._persist_candidates(db, temp_project, 2, [
            {"kind": "foreshadow_touch", "payload": {"foreshadow_id": "not-a-uuid",
                                                     "outcome": "resolved"}},
            {"kind": "foreshadow_touch", "payload": {"foreshadow_id": str(uuid.uuid4()),
                                                     "outcome": "resolved"}},
        ])
        db.flush()
    assert db_get_count_foreshadows(temp_project) == 0, "坏 id 不应落库任何东西"


def db_get_count_foreshadows(temp_project: str) -> int:
    with tenant_session(temp_project) as db:
        return db.query(Foreshadow).filter(
            Foreshadow.project_id == uuid.UUID(temp_project)).count()


def test_node_persist_awaiting_review_auto_persists_touch(temp_project):
    """critical 分流：foreshadow_touch 低风险自动回收，不进池、章节仍 awaiting_review。"""
    fs_id = _plant(temp_project)
    out = nodes.node_persist({
        "project_id": temp_project, "chapter_seq": 1, "draft": "正文",
        "candidates": [
            {"kind": "event", "confidence": 0.9,
             "payload": {"summary": "事件A", "participants": ["林砚"],
                         "source_chapter": 1, "confidence": 0.9}},
            {"kind": "foreshadow_touch", "confidence": 0.9,
             "payload": {"foreshadow_id": fs_id, "outcome": "advanced", "note": "本章推进"}},
        ],
        "report": {"summary": {"critical": 1, "l2_major": 0}},
    })
    assert out == {"persisted": True, "needs_review": True}
    assert _fs(temp_project, fs_id)["status"] == "developing", "分流时伏笔也应自动回收"
    with tenant_session(temp_project) as db:
        kinds = [c.kind for c in db.query(MemoryCandidate).filter(
            MemoryCandidate.project_id == uuid.UUID(temp_project)).all()]
        assert "event" in kinds and "foreshadow_touch" not in kinds, \
            f"foreshadow_touch 不应进待确认池，实际 {kinds}"
        ch = db.query(Chapter).filter(Chapter.project_id == uuid.UUID(temp_project),
                                      Chapter.chapter_seq == 1).first()
        assert ch is not None and ch.status == "awaiting_review"


def test_node_persist_auto_updates_touch(temp_project):
    """auto 放行路径：foreshadow_touch 自动落库，不因 skip_pool_handled 被跳过。"""
    fs_id = _plant(temp_project)
    out = nodes.node_persist({
        "project_id": temp_project, "chapter_seq": 1, "draft": "正文",
        "candidates": [
            {"kind": "foreshadow_touch", "confidence": 0.9,
             "payload": {"foreshadow_id": fs_id, "outcome": "resolved", "note": "真相揭晓"}},
        ],
        "report": {"summary": {"critical": 0, "l2_major": 0}},
    })
    assert out == {"persisted": True, "needs_review": False}
    assert _fs(temp_project, fs_id) == {"status": "resolved", "last_touched": 1,
                                        "resolved_chapter": 1}


def test_extract_messages_injects_open_foreshadows():
    """extract 输入注入开放伏笔块（foreshadow_touch 的 foreshadow_id 合法来源）。"""
    msgs = prompts.extract_messages("正文", 1, {
        "open_foreshadows": [{"foreshadow_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                              "status": "planted", "description": "玉佩之谜",
                              "trigger": {"actor": "林砚", "action": "发现", "object": "玉佩"},
                              "planted_chapter": 1}],
    })
    user = msgs[1]["content"]
    assert "【开放伏笔】" in user, "extract 应注入开放伏笔块"
    assert "(id: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)" in user, "伏笔应带 id 前缀"
    assert "foreshadow_touch" in msgs[0]["content"], "SYSTEM_EXTRACT 应声明 foreshadow_touch kind"
