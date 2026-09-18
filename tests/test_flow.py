"""端到端流程测试（mock LLM，不依赖真实 DeepSeek key）。

验证（conflict-samples.md 样例 1/3 的代码级落地）：
- 单章子图全链路：load→recall→plan→write→extract→validate→persist/needs_review；
- L1 校验能检出战力越界（林砚 realm_cap=金丹，extract 出元婴 → critical → 转人工）；
- 正常流：候选不越界 → persist 自动放行落库；
- 批次图：batch_plan → 单章 ×N → batch_end（汇总成本/耗时）；
- agent_runs 每节点记录（§6.8）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from aiink.db import new_session, tenant_session
from aiink.models import AgentRun, Chapter, Fact, Task
from aiink.providers.base import ModelProvider, ModelResponse
from aiink.workflow.batch_graph import build_batch_graph
from aiink.workflow.chapter_graph import build_chapter_graph
from aiink.workflow.runner import generate_batch, generate_chapter, get_graphs, new_task, resume_thread


class StubProvider(ModelProvider):
    """按 role/节点返回预置 JSON 的假 provider。"""

    def __init__(self, realm_from: str, realm_to: str):
        self.realm_from = realm_from
        self.realm_to = realm_to

    def name(self) -> str:
        return "stub"

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        user_content = "\n".join(m.get("content", "") for m in messages)
        node = self._infer_node(messages)
        if node == "cast":
            content = '{"cast":["林砚"],"locations":["黑市"]}'
        elif node == "plan":
            content = (
                '{"goals":["推进主线"],"scenes":[],"characters":[],'
                '"hooks_to_plant":[],"hooks_to_resolve":[],'
                '"expected_events":["林砚在黑市查探玉佩真相"],"hard_constraints":[]}'
            )
        elif node == "audit":
            # 审核中枢默认 pass（正常流自动放行）；异常分支由子类覆写
            content = (
                '{"verdict":"pass","findings":[],"reasons":["章节符合计划"],'
                '"confidence":0.9}'
            )
        elif node == "write":
            content = '林砚在黑市隐秘查探，玉佩气息若隐若现。夜色沉沉。'
        elif node == "extract":
            content = (
                '{"candidates":['
                '{"kind":"event","source_chapter":1,"confidence":0.9,"payload":{"summary":"林砚于黑市查探玉佩真相",'
                '"participants":["林砚"],"source_chapter":1,"confidence":0.9}},'
                f'{{"kind":"character_state","source_chapter":1,"confidence":0.9,"payload":{{"character_id":"林砚",'
                f'"field":"realm","old_value":"{self.realm_from}","new_value":"{self.realm_to}",'
                f'"source_chapter":1,"confidence":0.9}}}},'
                '{"kind":"foreshadow","source_chapter":1,"confidence":0.9,"payload":{"description":"玉佩气息异常，疑似与玉佩真相有关",'
                '"trigger":{"actor":"林砚","action":"发现","object":"玉佩"},"source_chapter":1,"confidence":0.9}}'
                ']}'
            )
        elif node == "revise":
            content = '=== CONTENT ===\n修订后正文\n=== RESPONSES ===\n[]'
        elif node == "summarize":
            content = '{"summary":"本章测试摘要"}'
        else:
            content = "{}"
        return ModelResponse(content=content, model_id=model_id, input_tokens=100,
                             output_tokens=200, duration_ms=50)


def _infer_node(messages):
    # 用 system 唯一标记判定节点（不能用泛词如"修订"——write 注入的召回上下文
    # 可能带旧章内容里的"修订"字样，会把 write 误判成 revise，stub 就永远不触发失败）
    sys = messages[0]["content"] if messages else ""
    if "出场人物 Agent" in sys:
        return "cast"
    if "规划 Agent" in sys:
        return "plan"
    if "记忆抽取" in sys:
        return "extract"
    if "修订 Agent" in sys:
        return "revise"
    if "审核中枢" in sys:
        return "audit"
    if "章节摘要" in sys:
        return "summarize"
    return "write"


StubProvider._infer_node = staticmethod(_infer_node)


@pytest.fixture
def project_id():
    """取 demo project（seed 已在 init 建立）。"""
    with new_session() as db:
        row = db.execute(text("SELECT id FROM projects WHERE title='九州问天'")).first()
        assert row is not None, "请先运行 `aiink init`"
        return str(row.id)


@pytest.fixture
def stub_provider(monkeypatch):
    import aiink.providers as providers_mod

    def _install(realm_from, realm_to):
        stub = StubProvider(realm_from, realm_to)
        monkeypatch.setattr(providers_mod, "default_provider", stub)
        return stub

    return _install


class FakeEmbedder:
    """假 bge-m3：encode 返回固定 1024 维向量（不走模型加载，测试快）。"""

    def encode(self, texts):
        return [[0.0] * 1024 for _ in texts]


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    """全测试 mock 掉 bge-m3：persist 向量化/recall/桥段近邻都走假实现，不加载真实模型。"""
    fake = FakeEmbedder()
    monkeypatch.setattr("aiink.workflow.nodes.get_embedder", lambda: fake)
    monkeypatch.setattr("aiink.memory.recall.get_embedder", lambda: fake)
    monkeypatch.setattr("aiink.validation.l1.get_embedder", lambda: fake)
    return fake


def _reset_runs(project_id):
    with new_session() as db:
        db.execute(text("DELETE FROM agent_runs WHERE project_id=:p"), {"p": project_id})
        db.commit()


def test_chapter_flow_auto_confirm(project_id, stub_provider):
    """正常流：候选不越界 → persist 自动放行落库（章节 confirmed）。"""
    stub_provider("金丹", "金丹")  # 林砚 金丹→金丹，不越界
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert result.get("needs_review") is False
    assert result.get("persisted") is True

    with tenant_session(project_id) as db:
        ch = db.execute(text("SELECT status FROM chapters WHERE chapter_seq=1")).scalar()
        assert ch == "confirmed", "自动放行应落库且章节 confirmed"
        ev = db.execute(text("SELECT count(*) FROM events WHERE source_chapter=1")).scalar()
        assert ev >= 1, "事件候选应落库"
        fs = db.execute(text("SELECT count(*) FROM foreshadows WHERE planted_chapter=1")).scalar()
        assert fs >= 1, "extract 应产伏笔候选且落库（§7.9 伏笔链路）"
        emb = db.execute(
            text("SELECT count(*) FROM embeddings WHERE project_id=:p AND level='event'"),
            {"p": project_id},
        ).scalar()
        assert emb >= 1, "事件向量应落库（§15 最小向量召回，bge-m3）"
        runs = db.query(AgentRun).filter(AgentRun.task_id == thread).all()
        nodes = {r.node for r in runs}
        assert {"plan_chapter", "write", "extract"} <= nodes, "agent_runs 应记录每节点"
        assert all(r.cost_est >= 0 for r in runs)


def test_rewrite_flow_invalidates_then_persists(project_id, stub_provider):
    """回归（2026-08-18）：重写路径曾以位置参数调 invalidate_chapter_memory（签名仅关键字）
    → TypeError 整章失败、旧记忆永不失效。invoke rewrite=True 应先失效再落库新正文。"""
    stub_provider("金丹", "金丹")
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread, "rewrite": True},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert result.get("persisted") is True, "重写应走 persist 自动放行落库"
    with tenant_session(project_id) as db:
        ch = db.execute(text("SELECT status FROM chapters WHERE chapter_seq=1")).scalar()
        assert ch == "confirmed", "重写完成后章节应 confirmed"


def test_recall_context_has_foreshadows(project_id, stub_provider, fake_embedder):
    """伏笔链路闭环：recall 上下文应带开放伏笔 + 活跃剧情线（§7.9 plan_chapter 输入）。"""
    from aiink.memory.recall import build_context

    stub_provider("金丹", "金丹")
    # 先跑一章种下伏笔（stub extract 产 foreshadow 候选）
    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    graph.invoke({"project_id": project_id, "chapter_seq": 1, "task_id": thread},
                 config={"configurable": {"thread_id": thread}})

    with tenant_session(project_id) as db:
        ctx = build_context(db, project_id=project_id, chapter_seq=2, participants=["林砚"])
    assert any("玉佩" in f["description"] for f in ctx.open_foreshadows), (
        "recall 应带刚种下的开放伏笔，实际 %s" % ctx.open_foreshadows
    )
    assert len(ctx.plot_threads) >= 1, "recall 应带活跃剧情线（seed 有 2 条）"


def test_chapter_flow_power_violation(project_id, stub_provider):
    """样例 1：林砚 金丹→元婴，超过 realm_cap=金丹 → L1 critical → needs_review 转人工。"""
    stub_provider("金丹", "元婴")
    _reset_runs(project_id)

    chapter_graph, _ = get_graphs()
    thread = str(uuid.uuid4())
    result = chapter_graph.invoke(
        {"project_id": project_id, "chapter_seq": 2, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None
    assert result.get("needs_review") is True, "越界应转人工确认"
    report = result.get("report") or {}
    types = {f["conflict_type"] for f in report.get("findings", [])}
    sev = {f["severity"] for f in report.get("findings", [])}
    assert "power" in types and "critical" in sev, f"应检出战力越界 critical，实际 {types}/{sev}"

    with tenant_session(project_id) as db:
        ch = db.execute(text("SELECT status FROM chapters WHERE chapter_seq=2")).scalar()
        assert ch == "awaiting_review", "critical 冲突章节应等待人工"


def _clean_seq2_memory(project_id):
    """自清理第 2 章的池/记忆/章节残留（本测试用第 2 章，防 demo 跨测试污染）。"""
    with tenant_session(project_id) as db:
        for tbl, col in [("memory_candidates", "source_chapter"), ("events", "source_chapter"),
                         ("character_states", "source_chapter"), ("foreshadows", "planted_chapter")]:
            db.execute(text(f"DELETE FROM {tbl} WHERE project_id=:p AND {col}=2"), {"p": project_id})
        db.execute(text("DELETE FROM chapters WHERE project_id=:p AND chapter_seq=2"), {"p": project_id})
        db.commit()


def test_confirm_resume_no_duplicate(project_id, stub_provider):
    """§6.11 confirm→resume 收尾：确认候选后 resume 落库正文，不重跑整章不重复沉淀。

    回归（2026-08-17）：确认后点续跑走确认流收尾——章节 awaiting_review → _dispatch
    的 chapter_resume 不重跑图（LangGraph 1.2.9 resume 语义=整图从 START 重跑，确定性
    输入会再命中 critical → 永久 awaiting_review、正文永不落库），而是取 checkpoint
    草稿直接落库正文 + 推进进度。已确认候选 confirm 时已落库，收尾不重复写、池不堆
    重复（评审 A2 幂等仍验：池内任意状态同位候选都挡重写）。
    """
    import aiink.workflow.nodes as nodes
    from aiink.models import MemoryCandidate
    from aiink.worker.processor import _dispatch

    stub_provider("金丹", "元婴")  # 越界 → L1 critical → 待确认池
    _reset_runs(project_id)
    _clean_seq2_memory(project_id)

    chapter_graph, _ = get_graphs()
    tid = str(uuid.uuid4())
    result = chapter_graph.invoke(
        {"project_id": project_id, "chapter_seq": 2, "task_id": tid},
        config={"configurable": {"thread_id": tid}},
    )
    assert result.get("needs_review") is True, "越界应转人工确认"

    with tenant_session(project_id) as db:
        pool = db.query(MemoryCandidate).filter(
            MemoryCandidate.project_id == uuid.UUID(project_id),
            MemoryCandidate.source_chapter == 2,
        ).all()
        assert pool, "critical 应产生待确认候选"
        assert all(c.status == "pending" for c in pool)
        ev_before = db.execute(text(
            "SELECT count(*) FROM events WHERE project_id=:p AND source_chapter=2"),
            {"p": project_id}).scalar()
        assert ev_before == 0, "critical 暂停时不应直接落库"
        runs_before = db.execute(text(
            "SELECT count(*) FROM agent_runs WHERE project_id=:p AND task_id=:t"),
            {"p": project_id, "t": tid}).scalar()

        # 人工确认 event 候选 → 落库 + 状态 confirmed（数据流边界 §6.2 编排层入口）
        ev_cand = next(c for c in pool if c.kind == "event")
        confirmed = nodes.confirm_candidate(db, project_id, ev_cand.id)
        assert confirmed is not None and confirmed.status == "confirmed"
        for remaining in pool:
            if remaining.status == "pending":
                remaining.status = "rejected"
                remaining.review = {"mode": "memory_only"}
        db.commit()

    with tenant_session(project_id) as db:
        ev_after = db.execute(text(
            "SELECT count(*) FROM events WHERE project_id=:p AND source_chapter=2"),
            {"p": project_id}).scalar()
        assert ev_after == 1, "人工确认应落库 1 条 event"

    # 收尾：worker 对 awaiting_review 章的 chapter_resume 路由到确认流收尾
    # （finalize_chapter_review 取 checkpoint 草稿 → node_persist auto 路径），
    # 不重跑图——agent_runs 只 +2 条（persist 落库 + summarize 追加摘要）。
    result = _dispatch({
        "project_id": project_id,
        "task_id": tid,
        "task_type": "chapter_resume",
        "payload": {"seq": 2},
    })
    assert result.get("needs_review") is False, f"收尾应放行（非转人工），实际 {result}"

    with tenant_session(project_id) as db:
        runs_after = db.execute(text(
            "SELECT count(*) FROM agent_runs WHERE project_id=:p AND task_id=:t"),
            {"p": project_id, "t": tid}).scalar()
        assert runs_after == runs_before + 2, \
            f"收尾应只补 persist+summarize 2 条运行记录（不重跑整章），实际 {runs_before} → {runs_after}"
        added_nodes = {r.node for r in db.query(AgentRun).filter(
            AgentRun.task_id == tid).order_by(AgentRun.id.asc()).all()[-2:]}
        assert added_nodes == {"persist", "summarize"}, \
            f"收尾应只跑 persist+summarize，实际 {added_nodes}"
        pool2 = db.query(MemoryCandidate).filter(
            MemoryCandidate.project_id == uuid.UUID(project_id),
            MemoryCandidate.source_chapter == 2,
        ).all()
        assert len(pool2) == len(pool), \
            f"收尾不应往池里堆重复候选（{len(pool)} → {len(pool2)}）"
        assert ("event", "confirmed") in [(c.kind, c.status) for c in pool2], \
            "确认的候选状态应保持 confirmed（不被覆盖/重复）"
        ev_final = db.execute(text(
            "SELECT count(*) FROM events WHERE project_id=:p AND source_chapter=2"),
            {"p": project_id}).scalar()
        assert ev_final == 1, "收尾不应重复落库 event"
        # 正文在收尾时落库（修复：confirm 后正文从未落库）
        row = db.execute(text(
            "SELECT status, content FROM chapters WHERE project_id=:p AND chapter_seq=2"),
            {"p": project_id}).fetchone()
        assert row is not None and row.status == "confirmed", \
            f"收尾后章节应 confirmed，实际 {row}"
        assert row.content, "收尾应落库正文"


def test_persist_skips_pool_handled_candidates(project_id):
    """auto 路径跳过已进确认池的候选（A2 第二道防线：skip_pool_handled 直测）。"""
    import aiink.workflow.nodes as nodes
    from aiink.models import MemoryCandidate

    payload = {
        "summary": "林砚于黑市查探玉佩真相", "participants": ["林砚"],
        "source_chapter": 3, "confidence": 0.9,
    }
    _clean_seq2_memory(project_id)
    # 单事务：中间不能 commit（SET LOCAL 事务级，commit 后 tenant 上下文丢失 → RLS 拦截 INSERT）
    with tenant_session(project_id) as db:
        db.execute(text("DELETE FROM events WHERE project_id=:p AND source_chapter=3"),
                   {"p": project_id})
        db.execute(text("DELETE FROM memory_candidates WHERE project_id=:p AND source_chapter=3"),
                   {"p": project_id})
        # 模拟确认流已落库的候选（confirmed）。
        # 必须显式 flush：_SessionLocal autoflush=False（§5.2），否则 SELECT 看不到
        # 未落库的候选，_pool_has_duplicate 返回 False 测不出拦截。flush 不结束事务，
        # SET LOCAL tenant 上下文仍在，RLS 放行。
        db.add(MemoryCandidate(project_id=uuid.UUID(project_id), kind="event",
                               source_chapter=3, payload=payload, confidence=0.9,
                               status="confirmed"))
        db.flush()
        # 相同 payload 走 auto 落库 → 应被池拦住，不重复写 event
        nodes._persist_candidates(db, project_id, 3,
                                  [{"kind": "event", "payload": payload, "confidence": 0.9}],
                                  skip_pool_handled=True)
        ev = db.execute(text(
            "SELECT count(*) FROM events WHERE project_id=:p AND source_chapter=3"),
            {"p": project_id}).scalar()
        assert ev == 0, "已进确认池的候选不应重复落库"


def test_batch_flow(project_id, stub_provider, monkeypatch):
    """批次：batch_plan（2 章）→ 单章 ×2 → batch_end 汇总。"""
    stub_provider("金丹", "金丹")

    import aiink.workflow.batch_graph as bg_mod

    # batch_plan 节点走 batch_graph.make_chain → 换批次 stub（返回 N 章蓝图）
    # 单章子图仍走 nodes.make_chain → default_provider（stub_provider 已换成单章 stub）
    batch_stub = BatchStubProvider("金丹", "金丹")
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(batch_stub))

    chapter_graph = build_chapter_graph()
    batch_graph = build_batch_graph(chapter_graph)
    thread = str(uuid.uuid4())
    result = batch_graph.invoke(
        {"project_id": project_id, "batch_task_id": thread, "size": 2,
         "position": 0, "start_chapter": 3},
        config={"configurable": {"thread_id": thread}},
    )
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "done", f"批次应完成，实际 {summary}"
    assert summary.get("completed") == 2, "应完成 2 章"


def _clean_chapter_range(project_id, lo: int, hi: int):
    """自清理某段章节的池/记忆/章节残留（批次测试复用范围前先清，防跨测试污染）。"""
    with tenant_session(project_id) as db:
        for tbl, col in [("memory_candidates", "source_chapter"), ("events", "source_chapter"),
                         ("character_states", "source_chapter"), ("foreshadows", "planted_chapter")]:
            db.execute(text(f"DELETE FROM {tbl} WHERE project_id=:p AND {col} BETWEEN :lo AND :hi"),
                       {"p": project_id, "lo": lo, "hi": hi})
        db.execute(text("DELETE FROM chapters WHERE project_id=:p AND chapter_seq BETWEEN :lo AND :hi"),
                   {"p": project_id, "lo": lo, "hi": hi})
        db.commit()


def test_batch_review_resume_finalizes_chapter(project_id, monkeypatch):
    """§6.11 批次确认流：critical 章暂停 → 确认候选后 resume 收尾落库正文，不重跑本章。

    回归（2026-08-17）：批次 critical 暂停后人工确认候选、resume 批次——node_run_chapter
    若重跑本章（LangGraph 1.2.9 resume=整图从 START 重跑），确定性输入会再命中 critical
    → 永久暂停、正文永不落库（用户实测：确认后点续跑变成重新生成）。修后取本章
    checkpoint 草稿直接收尾落库正文（不重跑图），再续下一章；下一章仍 critical 则另行
    暂停等人工。用 6/7 章段（3/4/5 被他批次测试复用），结束自清理防污染。
    """
    import aiink.providers as providers_mod
    import aiink.workflow.batch_graph as bg_mod
    import aiink.workflow.nodes as nodes
    from aiink.models import MemoryCandidate

    _clean_chapter_range(project_id, 6, 7)
    stub = BatchStubProvider("金丹", "元婴")  # 每章越界 → L1 critical
    monkeypatch.setattr(providers_mod, "default_provider", stub)          # 单章节点链
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))  # batch_plan 节点
    _reset_runs(project_id)

    tid = new_task(project_id=project_id, task_type="batch_generate",
                   payload={"start": 6, "size": 2})
    result = generate_batch(project_id=project_id, size=2, start_chapter=6, batch_task_id=tid)
    assert result.get("needs_review") is True, f"批次应在 ch6 critical 暂停，实际 {result}"

    with tenant_session(project_id) as db:
        ch6 = db.execute(text("SELECT status, content FROM chapters WHERE project_id=:p AND chapter_seq=6"),
                         {"p": project_id}).fetchone()
        assert ch6 is not None and ch6.status == "awaiting_review", f"ch6 应待人工，实际 {ch6}"
        assert ch6.content, "待确认时已生成正文也必须立即展示"
        pool = db.query(MemoryCandidate).filter(
            MemoryCandidate.project_id == uuid.UUID(project_id),
            MemoryCandidate.source_chapter == 6,
        ).all()
        assert pool, "critical 应产生待确认候选"
        ev_cand = next(c for c in pool if c.kind == "event")
        assert nodes.confirm_candidate(db, project_id, ev_cand.id) is not None
        for remaining in pool:
            if remaining.status == "pending":
                remaining.status = "rejected"
                remaining.review = {"mode": "memory_only"}
        db.commit()

    # resume 批次：ch6 确认流收尾落库正文（不重跑 ch6），推进到 ch7 再 critical 暂停
    # （BatchReviewError 由 worker 的 _run 接住置 awaiting_review；直调 resume_thread 会抛出）
    _, batch_graph = get_graphs()
    with pytest.raises(bg_mod.BatchReviewError):
        resume_thread(batch_graph, tid, {
            "project_id": project_id, "batch_task_id": tid, "size": 2,
            "position": 0, "start_chapter": 6,
        })

    with tenant_session(project_id) as db:
        ch6 = db.execute(text("SELECT status, content FROM chapters WHERE project_id=:p AND chapter_seq=6"),
                         {"p": project_id}).fetchone()
        assert ch6 is not None and ch6.status == "confirmed", f"ch6 收尾后应 confirmed，实际 {ch6}"
        assert ch6.content, "ch6 收尾应落库正文"
        ch7 = db.execute(text("SELECT status FROM chapters WHERE project_id=:p AND chapter_seq=7"),
                         {"p": project_id}).scalar()
        assert ch7 == "awaiting_review", "ch7 应再次暂停等人工"
    _clean_chapter_range(project_id, 6, 7)


def test_batch_plan_short_explicit_fail(project_id, monkeypatch):
    """批次规划少返回 → 显式失败（评审 A3），而不是写了部分章却静默收尾。"""
    import aiink.providers as providers_mod
    import aiink.workflow.batch_graph as bg_mod

    stub = ShortBatchStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))
    _reset_runs(project_id)

    chapter_graph = build_chapter_graph()
    batch_graph = build_batch_graph(chapter_graph)
    thread = str(uuid.uuid4())
    result = batch_graph.invoke(
        {"project_id": project_id, "batch_task_id": thread, "size": 2,
         "position": 0, "start_chapter": 3},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("batch_failed") is True, "少返回应显式失败批次"
    assert "只返回 1 项" in result.get("error", ""), f"应报缺项，实际 {result.get('error')}"
    # batch_plan 失败 → fail 边 → batch_end 收尾，但汇总必须是 failed 且 0 完成
    # （失败路径也要走 batch_end 记一笔 failed 汇总，而不是裸中断；§A3）
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "failed", f"失败批次汇总应为 failed，实际 {summary}"
    assert summary.get("completed") == 0, "规划阶段失败不应有任何章完成"


def test_batch_failure_and_resume(project_id, monkeypatch):
    """§6.12 批次失败续跑（spec/state-flow.md §5：从失败章续跑，不重跑已完成章）。

    第 2 章 write 失败 → generate_batch 抛 BatchChapterError 中断（非 END）、
    任务 failed；resume_thread 同 thread 续跑 → 失败章重试成功 → 批次 done，
    ch3（已完成）write 不重跑、ch4（失败）write 恰好重试一次。
    """
    import aiink.providers as providers_mod
    import aiink.workflow.batch_graph as bg_mod

    stub = BatchFailStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)          # 单章节点链
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))  # batch_plan 节点
    # writer 降级链打成单模型：write 失败一次即定（fallback 会掩盖失败、干扰计数语义）
    monkeypatch.setitem(providers_mod.DEFAULT_ROUTES, "writer", ["deepseek-v4-flash"])

    tid = new_task(project_id=project_id, task_type="batch_generate",
                   payload={"start": 3, "size": 2})
    result = generate_batch(project_id=project_id, size=2, start_chapter=3, batch_task_id=tid)
    assert result.get("batch_failed") is True, f"批次应中断失败，实际 {result}"
    assert "单章 4 失败" in result.get("error", ""), result
    assert stub.write_calls == 2, f"首次批次应 ch3(1)+ch4(1)=2 次 write，实际 {stub.write_calls}"
    with new_session() as db:
        assert db.get(Task, uuid.UUID(tid)).status == "failed", "批次失败任务应为 failed"

    _, batch_graph = get_graphs()
    final = resume_thread(batch_graph, tid, {
        "project_id": project_id, "batch_task_id": tid, "size": 2,
        "position": 0, "start_chapter": 3,  # position=0 故意传旧值：续跑应以 checkpoint 为准
    })
    summary = final.get("batch_summary") or {}
    assert summary.get("status") == "done", f"续跑后批次应完成，实际 {summary}"
    assert summary.get("completed") == 2
    assert stub.write_calls == 3, f"已完成章不应重跑（续跑只补 ch4 一次 write），实际 {stub.write_calls}"
    with new_session() as db:
        assert db.get(Task, uuid.UUID(tid)).status == "done", "续跑成功任务应为 done"

    with tenant_session(project_id) as db:
        w3 = db.execute(text(
            "SELECT count(*) FROM agent_runs WHERE task_id LIKE :p AND node='write'"),
            {"p": f"{tid}:ch3%"}).scalar()
        w4 = db.execute(text(
            "SELECT count(*) FROM agent_runs WHERE task_id LIKE :p AND node='write'"),
            {"p": f"{tid}:ch4%"}).scalar()
        assert w3 == 1, "已完成章 ch3 的 write 不应重跑"
        assert w4 == 2, "失败章 ch4 应恰好重试一次（1 失败 + 1 成功）"


def test_batch_pause_halt_and_resume(project_id, monkeypatch):
    """§6.12 手动暂停：章节边界守卫中断批次（不写剩余章），resume 同 thread 续跑不重跑已完成章。

    批次 size=3（ch3/4/5），第 2 次 write（ch4）期间用户点暂停 → 任务置 paused；
    图在 ch5 边界读到 paused → BatchHaltError 中断（非 END，checkpoint 停在本章）。
    断言：write 只到 ch4（2 次）、任务保持 paused；resume 后只补 ch5（write=3）→ done。
    """
    import aiink.providers as providers_mod
    import aiink.workflow.batch_graph as bg_mod

    tid = new_task(project_id=project_id, task_type="batch_generate",
                   payload={"start": 3, "size": 3})

    def pause_batch():
        with tenant_session(project_id) as db:
            db.get(Task, uuid.UUID(tid)).status = "paused"
            db.commit()

    stub = BatchHaltStub("金丹", "金丹", on_write=2, halt_cb=pause_batch)
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))
    monkeypatch.setitem(providers_mod.DEFAULT_ROUTES, "writer", ["deepseek-v4-flash"])
    _reset_runs(project_id)

    result = generate_batch(project_id=project_id, size=3, start_chapter=3, batch_task_id=tid)
    assert result.get("manual_halt") == "paused", f"应返回 manual_halt=paused，实际 {result}"
    assert stub.write_calls == 2, f"暂停后应停在第 3 章前（write=2），实际 {stub.write_calls}"
    with new_session() as db:
        assert db.get(Task, uuid.UUID(tid)).status == "paused", "暂停后任务状态应保持 paused"

    # resume：worker 拾取语义把 paused 置回 running + 同 thread 续跑 → 补跑剩余章 → done
    with tenant_session(project_id) as db:
        db.get(Task, uuid.UUID(tid)).status = "running"
        db.commit()
    _, batch_graph = get_graphs()
    final = resume_thread(batch_graph, tid, {
        "project_id": project_id, "batch_task_id": tid, "size": 3,
        "position": 0, "start_chapter": 3,  # position=0 故意传旧值：续跑以 checkpoint 为准
    })
    summary = final.get("batch_summary") or {}
    assert summary.get("status") == "done", f"续跑后批次应完成，实际 {summary}"
    assert stub.write_calls == 3, f"已完成章不应重跑（续跑只补第 3 章 write），实际 {stub.write_calls}"
    with new_session() as db:
        assert db.get(Task, uuid.UUID(tid)).status == "done", "续跑成功任务应为 done"


def test_batch_cancel_halt(project_id, monkeypatch):
    """§6.12 手动取消：边界守卫中断批次，cancelled 保持（不被 batch_end 覆盖回 done）。

    同暂停机制（BatchHaltError），区别在终态语义：取消后任务置 cancelled、无续跑。
    """
    import aiink.providers as providers_mod
    import aiink.workflow.batch_graph as bg_mod

    tid = new_task(project_id=project_id, task_type="batch_generate",
                   payload={"start": 3, "size": 3})

    def cancel_batch():
        with tenant_session(project_id) as db:
            db.get(Task, uuid.UUID(tid)).status = "cancelled"
            db.commit()

    stub = BatchHaltStub("金丹", "金丹", on_write=2, halt_cb=cancel_batch)
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))
    monkeypatch.setitem(providers_mod.DEFAULT_ROUTES, "writer", ["deepseek-v4-flash"])
    _reset_runs(project_id)

    result = generate_batch(project_id=project_id, size=3, start_chapter=3, batch_task_id=tid)
    assert result.get("manual_halt") == "cancelled", f"应返回 manual_halt=cancelled，实际 {result}"
    assert stub.write_calls == 2, f"取消后应停在第 3 章前（write=2），实际 {stub.write_calls}"
    with new_session() as db:
        assert db.get(Task, uuid.UUID(tid)).status == "cancelled", "取消后任务状态应保持 cancelled"


def test_task_status_done(project_id, stub_provider):
    """runner.generate_chapter 成功后任务终态应为 done（§6.8 任务状态可观测）。"""
    stub_provider("金丹", "金丹")
    tid = new_task(project_id=project_id, task_type="chapter_generate",
                   payload={"seq": 11}, chapter_seq=11)
    r = generate_chapter(project_id=project_id, chapter_seq=11, task_id=tid)
    assert r.get("error") is None, r.get("error")
    with new_session() as db:
        assert db.get(Task, uuid.UUID(tid)).status == "done"


def test_audit_rewrite_loop(project_id, monkeypatch):
    """审核中枢驱动修订：audit 判 rewrite → revise 逐条修 → 回 audit pass → persist。

    验证 §6.5（触发源从校验报告改为审核中枢 verdict）+ 混合路由 rewrite 分支。
    """
    import aiink.providers as providers_mod

    stub = AuditRewriteStub("金丹", "金丹", rewrite_times=1)
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert stub.audit_calls == 2, f"audit 应跑 2 次（首判 rewrite + 修订后 pass），实际 {stub.audit_calls}"
    assert stub.revise_calls == 1, "rewrite 应触发 1 次 revise"
    assert result.get("persisted") is True
    assert (result.get("audit_verdict") or {}).get("verdict") == "pass"


def test_audit_replan_chapter(project_id, monkeypatch):
    """审核判 replan(chapter) → 回 plan_chapter 重规划本章 → 重写 → pass → persist。

    验证混合路由 replan 分支 + replan_count 预算。
    """
    import aiink.providers as providers_mod

    stub = AuditReplanStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert stub.audit_calls == 2, f"audit 应跑 2 次（首判 replan + 重规划后 pass），实际 {stub.audit_calls}"
    assert stub.plan_calls == 2, "replan 后 plan_chapter 应重跑一次（共 2 次）"
    assert result.get("replan_count") == 1, "replan_count 应为 1"
    assert result.get("persisted") is True


def test_batch_shared_context(project_id, monkeypatch):
    """批次级共享上下文（§6.11 共享池）：hard_facts 稳定部分批次内只组装一次。

    用 repo.get_hard_facts 调用计数验证——2 章批次只查 1 次硬约束，第 2 章复用缓存。
    """
    import aiink.providers as providers_mod
    import aiink.memory.recall as recall_mod
    import aiink.workflow.batch_graph as bg_mod

    stub = BatchStubProvider("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))

    original = recall_mod.repo.get_hard_facts
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(recall_mod.repo, "get_hard_facts", counting)

    chapter_graph = build_chapter_graph()
    batch_graph = build_batch_graph(chapter_graph)
    thread = str(uuid.uuid4())
    result = batch_graph.invoke(
        {"project_id": project_id, "batch_task_id": thread, "size": 2,
         "position": 0, "start_chapter": 3},
        config={"configurable": {"thread_id": thread}},
    )
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "done", f"批次应完成，实际 {summary}"
    assert summary.get("completed") == 2
    assert calls["n"] == 1, (
        f"hard_facts 批次内应只查 1 次（第 1 章组装、第 2 章复用共享池），实际 {calls['n']}"
    )


def test_audit_replan_batch(project_id, monkeypatch):
    """批次蓝图走偏：第 1 章 audit 判 replan(batch) → 批次层回 batch_plan 重规划 → 剩余章照跑。

    验证 §6.11 replan 自适应（batch 粒度）+ 混合路由的 replan_batch 批次边。
    """
    import aiink.providers as providers_mod
    import aiink.workflow.batch_graph as bg_mod

    stub = AuditReplanBatchStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    monkeypatch.setattr(bg_mod, "make_chain", lambda role, **_kwargs: _Chain(stub))
    _reset_runs(project_id)

    chapter_graph = build_chapter_graph()
    batch_graph = build_batch_graph(chapter_graph)
    thread = str(uuid.uuid4())
    result = batch_graph.invoke(
        {"project_id": project_id, "batch_task_id": thread, "size": 2,
         "position": 0, "start_chapter": 3},
        config={"configurable": {"thread_id": thread}},
    )
    summary = result.get("batch_summary") or {}
    assert summary.get("status") == "done", f"批次应完成，实际 {summary}"
    assert stub.batch_plan_calls == 2, f"batch_plan 应重规划一次（共 2 次），实际 {stub.batch_plan_calls}"
    assert stub.audit_calls >= 3, f"audit 应跑（ch3 首判 replan-batch + ch3 重跑 + ch4），实际 {stub.audit_calls}"


def test_audit_rewrite_loop_content_driven(project_id, monkeypatch):
    """审核中枢真实闭环：audit 基于**草稿内容**判 rewrite（非调用次数硬编码）→
    revise 注入 findings 执行修复 → 修订稿送回 audit 复验 pass → 落库的是修订稿。

    比 test_audit_rewrite_loop 严格：后者 stub 按次数返回（第 1 次 rewrite 第 2 次 pass），
    无法证明「真的修了、真的复验通过」；本测试路由由草稿是否含修订标记决定。
    """
    import aiink.providers as providers_mod

    stub = ContentAuditRewriteStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert stub.audit_calls == 2, f"audit 应 2 次（首判 rewrite + 修订稿复验 pass），实际 {stub.audit_calls}"
    assert stub.revise_calls == 1, "rewrite 应真实执行 1 次 revise"
    assert (result.get("audit_verdict") or {}).get("verdict") == "pass"
    assert result.get("persisted") is True
    with tenant_session(project_id) as db:
        content = db.execute(text("SELECT content FROM chapters WHERE chapter_seq=1")).scalar()
        assert "已修订" in content, "落库应为修订后草稿（revise 修复真实生效）"


def test_audit_replan_chapter_content_driven(project_id, monkeypatch):
    """审核中枢真实闭环 replan：audit 基于**草稿内容**判 replan（非次数硬编码）→
    reset_replan 清瞬态 → plan_chapter 重规划 → write 重写（新稿）→ 复验 pass → 落库新稿。

    比 test_audit_replan_chapter 严格：路由由草稿是否含重规划新稿标记决定，
    验证「判 replan → 真实重规划重写 → 复验通过」。
    """
    import aiink.providers as providers_mod

    stub = ContentAuditReplanStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert stub.audit_calls == 2, f"audit 应 2 次（首判 replan + 重规划后复验 pass），实际 {stub.audit_calls}"
    assert stub.plan_calls == 2, "replan 后 plan_chapter 应重规划一次（共 2 次）"
    assert stub.write_calls == 2, "重规划后应真实重写一次（共 2 次）"
    assert result.get("replan_count") == 1
    assert (result.get("audit_verdict") or {}).get("verdict") == "pass"
    with tenant_session(project_id) as db:
        content = db.execute(text("SELECT content FROM chapters WHERE chapter_seq=1")).scalar()
        assert "重规划新稿" in content, "落库应为重规划后新稿"


class BatchStubProvider(StubProvider):
    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if "批次规划" in (messages[0].get("content", "") if messages else ""):
            content = '{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"},{"goal":"推进主线B","outline_advance":"卷纲推进"}]}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)
        user_content = "\n".join(m.get("content", "") for m in messages)
        if "批次规划" in messages[0].get("content", ""):
            content = '{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"},{"goal":"推进主线B","outline_advance":"卷纲推进"}]}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class ShortBatchStub(BatchStubProvider):
    """批次规划少返回：size=2 但只回 1 章（测 A3 强制 N 项校验）。"""

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if "批次规划" in (messages[0].get("content", "") if messages else ""):
            content = '{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"}]}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=50,
                                 output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class _Chain:
    def __init__(self, provider):
        self.provider = provider

    def generate(self, messages, *, json_mode=False, max_tokens=None, temperature=None, tools=None):
        return self.provider.generate(messages, model_id="stub", max_tokens=max_tokens,
                                      temperature=temperature, json_mode=json_mode, tools=tools)


class BatchFailStub(StubProvider):
    """批次 write 第 N 次调用返回 error（测 §6.12 批次失败续跑）。

    fail_on_write=2：批次第 1 章 write 成功、第 2 章 write 失败 → 批次中断。
    failures 只计一次——续跑（同 thread 再 invoke）时该章 write 重跑即成功，
    用来断言「已完成章不重跑、失败章重试」。
    """

    def __init__(self, realm_from, realm_to, fail_on_write=2):
        super().__init__(realm_from, realm_to)
        self.fail_on_write = fail_on_write
        self.write_calls = 0
        self.failures = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if "批次规划" in (messages[0].get("content", "") if messages else ""):
            content = '{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"},{"goal":"推进主线B","outline_advance":"卷纲推进"}]}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=50,
                                 output_tokens=80, duration_ms=30)
        if self._infer_node(messages) == "write":
            self.write_calls += 1
            if self.failures == 0 and self.write_calls == self.fail_on_write:
                self.failures += 1
                return ModelResponse(content="", model_id=model_id, error="write 失败",
                                     input_tokens=100, output_tokens=0, duration_ms=50)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class BatchHaltStub(StubProvider):
    """批次中途手动暂停/取消：第 on_write 次 write 期间触发 halt_cb（模拟用户途中点暂停/取消）。

    测 §6.12 批次控制守卫：章节边界查 DB 任务状态 → paused/cancelled 抛 BatchHaltError
    中断（剩余章不写），resume 同 thread 续跑补跑剩余章、已完成章不重跑。
    """

    def __init__(self, realm_from, realm_to, on_write=2, halt_cb=None):
        super().__init__(realm_from, realm_to)
        self.on_write = on_write
        self.halt_cb = halt_cb
        self.write_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if "批次规划" in (messages[0].get("content", "") if messages else ""):
            content = ('{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"},'
                       '{"goal":"推进主线B","outline_advance":"卷纲推进"},'
                       '{"goal":"推进主线C","outline_advance":"卷纲推进"}]}')
            return ModelResponse(content=content, model_id=model_id, input_tokens=50,
                                 output_tokens=80, duration_ms=30)
        if self._infer_node(messages) == "write":
            self.write_calls += 1
            if self.write_calls == self.on_write and self.halt_cb:
                self.halt_cb()  # 模拟用户在第 on_write 次 write 期间点暂停/取消
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class AuditRewriteStub(StubProvider):
    """审核中枢前 N 次判 rewrite，之后 pass（测 §6.5 修订循环由 verdict 驱动）。

    audit 判 rewrite 时带 L2 finding（critical/major → unresolved），
    revise 注入逐条修 → 回 audit → 本次 pass → persist。
    """

    def __init__(self, realm_from, realm_to, rewrite_times=1):
        super().__init__(realm_from, realm_to)
        self.rewrite_times = rewrite_times
        self.audit_calls = 0
        self.revise_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        node = self._infer_node(messages)
        if node == "audit":
            self.audit_calls += 1
            if self.audit_calls <= self.rewrite_times:
                content = (
                    '{"verdict":"rewrite","findings":['
                    '{"conflict_key":"a1","conflict_type":"plotline","severity":"major",'
                    '"scope":"structural","evidence":[{"chapter":1,"quote":"主线偏移"}],'
                    '"confidence":0.8,"suggestion":"拉回主线"}],'
                    '"reasons":["本章主线偏移"],"confidence":0.85}'
                )
            else:
                content = '{"verdict":"pass","findings":[],"reasons":["修订后达标"],"confidence":0.9}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        if node == "revise":
            self.revise_calls += 1
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class AuditReplanStub(StubProvider):
    """审核判 replan(chapter) 一次，重规划后 pass（测 §6.5 replan 回 plan_chapter）。"""

    def __init__(self, realm_from, realm_to):
        super().__init__(realm_from, realm_to)
        self.audit_calls = 0
        self.plan_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        node = self._infer_node(messages)
        if node == "audit":
            self.audit_calls += 1
            if self.audit_calls == 1:
                content = (
                    '{"verdict":"replan","replan_target":"chapter","findings":[],'
                    '"reasons":["本章未按蓝图推进"],"confidence":0.8}'
                )
            else:
                content = '{"verdict":"pass","findings":[],"reasons":["重规划后达标"],"confidence":0.9}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        if node == "plan":
            self.plan_calls += 1
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class AuditReplanBatchStub(StubProvider):
    """审核判 replan(batch)（测 §6.11：批次蓝图走偏 → 回 batch_plan 重规划剩余章）。"""

    def __init__(self, realm_from, realm_to):
        super().__init__(realm_from, realm_to)
        self.audit_calls = 0
        self.batch_plan_calls = 0
        self.bridge_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if "批次规划" in (messages[0].get("content", "") if messages else ""):
            self.batch_plan_calls += 1
            content = '{"chapters":[{"goal":"推进主线A","outline_advance":"卷纲推进"},{"goal":"推进主线B","outline_advance":"卷纲推进"}]}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=50,
                                 output_tokens=80, duration_ms=30)
        if self._infer_node(messages) == "audit":
            self.audit_calls += 1
            if self.audit_calls == 1:
                # 第 1 章 audit 判 replan(batch)：蓝图整体走偏，需重规划剩余章
                content = (
                    '{"verdict":"replan","replan_target":"batch","findings":[],'
                    '"reasons":["批次蓝图整体偏离主线"],"confidence":0.8}'
                )
            else:
                content = '{"verdict":"pass","findings":[],"reasons":["重规划后达标"],"confidence":0.9}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class ContentAuditRewriteStub(StubProvider):
    """内容驱动审核（rewrite 闭环）：audit 看草稿是否含「已修订」标记，含则 pass，不含则 rewrite。

    路由由**草稿内容**决定（而非调用次数硬编码）——证明 audit 判 rewrite 后，
    revise 真的执行修复、修订稿真的送回 audit 复验、pass 才落库。
    """

    def __init__(self, realm_from, realm_to):
        super().__init__(realm_from, realm_to)
        self.audit_calls = 0
        self.revise_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        node = self._infer_node(messages)
        if node == "audit":
            self.audit_calls += 1
            text = "\n".join(m.get("content", "") for m in messages)
            if "已修订" in text:
                content = '{"verdict":"pass","findings":[],"reasons":["修订后达标"],"confidence":0.9}'
            else:
                content = (
                    '{"verdict":"rewrite","findings":['
                    '{"conflict_key":"a1","conflict_type":"plotline","severity":"major",'
                    '"scope":"structural","evidence":[{"chapter":1,"quote":"主线偏移"}],'
                    '"confidence":0.8,"suggestion":"拉回主线"}],'
                    '"reasons":["本章主线偏移"],"confidence":0.85}'
                )
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        if node == "revise":
            self.revise_calls += 1
            # 修订稿打上「已修订」标记——audit 据此复验放行
            content = ('=== CONTENT ===\n林砚在黑市隐秘查探，玉佩气息若隐若现。夜色沉沉。已修订\n'
                       '=== RESPONSES ===\n[{"conflict_key":"a1","status":"fixed"}]')
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class ContentAuditReplanStub(StubProvider):
    """内容驱动审核（replan 闭环）：audit 看草稿是否含「重规划新稿」标记，含则 pass，不含则 replan。

    首轮 write 产旧稿 → audit 判 replan(chapter) → reset_replan 清瞬态 →
    plan_chapter 重规划 → write 重写（新稿）→ audit 复验 pass。plan/write 调用计数
    证明重规划链路真实执行，落库断言证明放行的是新稿。
    """

    def __init__(self, realm_from, realm_to):
        super().__init__(realm_from, realm_to)
        self.audit_calls = 0
        self.plan_calls = 0
        self.write_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        node = self._infer_node(messages)
        if node == "audit":
            self.audit_calls += 1
            text = "\n".join(m.get("content", "") for m in messages)
            if "重规划新稿" in text:
                content = '{"verdict":"pass","findings":[],"reasons":["重规划后达标"],"confidence":0.9}'
            else:
                content = (
                    '{"verdict":"replan","replan_target":"chapter","findings":[],'
                    '"reasons":["本章未按蓝图推进"],"confidence":0.8}'
                )
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        if node == "plan":
            self.plan_calls += 1
        if node == "write":
            self.write_calls += 1
            if self.write_calls == 1:
                # 首轮：旧稿（不含标记）→ 触发 replan
                content = '林砚在黑市隐秘查探，玉佩气息若隐若现。夜色沉沉。'
            else:
                # 重规划后：新稿带标记 → audit 复验 pass
                content = '林砚在黑市隐秘查探，玉佩气息若隐若现。夜色沉沉。重规划新稿'
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class ToolAuditStub(StubProvider):
    """audit 工具循环（§10）：工具轮调 inspect_character 核实人物，工具结果回传后 pass。

    路由由「消息历史是否含 tool 结果」决定（内容驱动，非次数硬编码）——
    证明工具结果真实回传、模型基于工具输出收敛到最终 verdict。
    """

    def __init__(self, realm_from, realm_to):
        super().__init__(realm_from, realm_to)
        self.audit_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if self._infer_node(messages) == "audit":
            self.audit_calls += 1
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            if tools and not has_tool_result:
                return ModelResponse(content="", model_id=model_id, input_tokens=60,
                                     output_tokens=10, duration_ms=30,
                                     tool_calls=[{"id": "call_1", "name": "inspect_character",
                                                  "arguments": {"name": "林砚"}}])
            content = '{"verdict":"pass","findings":[],"reasons":["核实后达标"],"confidence":0.9}'
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class ToolWriteStub(StubProvider):
    """write 工具循环（§10）：工具轮调 inspect_facts 核实硬约束，工具结果回传后出稿。"""

    def __init__(self, realm_from, realm_to):
        super().__init__(realm_from, realm_to)
        self.write_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if self._infer_node(messages) == "write":
            self.write_calls += 1
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            if tools and not has_tool_result:
                return ModelResponse(content="", model_id=model_id, input_tokens=60,
                                     output_tokens=10, duration_ms=30,
                                     tool_calls=[{"id": "call_w", "name": "inspect_facts",
                                                  "arguments": {}}])
            content = '林砚在黑市隐秘查探，玉佩气息若隐若现。夜色沉沉。'
            return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                 output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


class ToolBudgetStub(StubProvider):
    """工具预算封顶（§10）：audit 工具轮永远要求调工具（耗光预算），最终轮才 pass。

    验证 max_tool_calls=3 封顶：第 3 次工具执行后强制进无 tools 最终轮。
    """

    def __init__(self, realm_from, realm_to):
        super().__init__(realm_from, realm_to)
        self.audit_calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if self._infer_node(messages) == "audit":
            self.audit_calls += 1
            if json_mode:
                # 最终轮（预算用尽后）：无 tools + json_mode，输出最终 verdict
                content = '{"verdict":"pass","findings":[],"reasons":["核实后达标"],"confidence":0.9}'
                return ModelResponse(content=content, model_id=model_id, input_tokens=60,
                                     output_tokens=80, duration_ms=30)
            # 工具轮：永远要求调工具（耗光预算，验证封顶后强制收敛）
            return ModelResponse(content="", model_id=model_id, input_tokens=60,
                                 output_tokens=10, duration_ms=30,
                                 tool_calls=[{"id": "call_t", "name": "inspect_plot_threads",
                                              "arguments": {}}])
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


def test_audit_tool_loop(project_id, monkeypatch):
    """audit 持只读查证工具（§10）：工具轮调 inspect_character → 结果回传 → pass。

    验证 _run_tool_loop：单工具轮 + 条件最终轮、每轮 agent_runs 记录、tool_trace 回写。
    """
    import aiink.providers as providers_mod

    stub = ToolAuditStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert stub.audit_calls == 2, f"audit 应 2 轮（1 工具轮 + 1 最终轮），实际 {stub.audit_calls}"
    assert (result.get("audit_verdict") or {}).get("verdict") == "pass"
    assert result.get("persisted") is True
    trace = result.get("tool_trace") or []
    assert len(trace) == 1, f"tool_trace 应 1 条工具调用，实际 {trace}"
    assert trace[0]["tool"] == "inspect_character" and trace[0]["arguments"] == {"name": "林砚"}, trace
    with tenant_session(project_id) as db:
        audit_runs = db.query(AgentRun).filter(AgentRun.task_id == thread,
                                               AgentRun.node == "audit").count()
        assert audit_runs == 2, f"audit 每轮 LLM 调用都应记 agent_runs，实际 {audit_runs}"


def test_write_tool_loop(project_id, monkeypatch):
    """write 持只读查证工具（§10）：工具轮调 inspect_facts → 结果回传 → 出稿落库。"""
    import aiink.providers as providers_mod

    stub = ToolWriteStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert stub.write_calls == 2, f"write 应 2 轮（1 工具轮 + 1 最终轮），实际 {stub.write_calls}"
    assert result.get("persisted") is True
    trace = result.get("tool_trace") or []
    assert len(trace) == 1 and trace[0]["tool"] == "inspect_facts", trace
    with tenant_session(project_id) as db:
        content = db.execute(text("SELECT content FROM chapters WHERE chapter_seq=1")).scalar()
        assert "玉佩" in content, "write 出稿应落库"
        write_runs = db.query(AgentRun).filter(AgentRun.task_id == thread,
                                               AgentRun.node == "write").count()
        assert write_runs == 2, f"write 每轮 LLM 调用都应记 agent_runs，实际 {write_runs}"


class WriteNoiseStub(StubProvider):
    """write 跑偏（§10）：模型把工具调用写成文本而非 function calling → 噪声判失败。"""

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        messages = list(messages)
        if self._infer_node(messages) == "write":
            return ModelResponse(
                content='<ai_output>\n<function_results>\n<invoke name="inspect_facts">\n'
                        '</function_results>\n</ai_output>',
                model_id=model_id, input_tokens=60, output_tokens=80, duration_ms=30)
        return super().generate(messages, model_id=model_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)


def test_write_rejects_tool_noise(project_id, monkeypatch):
    """write 输出文本式工具调用（§10 跑偏）：显式失败，不把噪声当正文落库。"""
    import aiink.providers as providers_mod

    stub = WriteNoiseStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    err = result.get("error") or ""
    assert "跑偏" in err, f"write 工具噪声应判失败，实际 {err!r}"
    with tenant_session(project_id) as db:
        content = db.execute(text("SELECT content FROM chapters WHERE chapter_seq=1")).scalar()
        assert content is None or "inspect_facts" not in (content or ""), "噪声不应落库"


def test_tool_budget_forced_exit(temp_project, monkeypatch):
    """工具预算封顶（§10）：stub 每轮都要求调工具 → 3 次执行后用尽预算 → 强制最终轮。

    验证 max_tool_calls=3 封顶：tool_trace 恰好 3 条、audit 4 轮 LLM（3 工具 + 1 最终）
    仍正常 pass 收敛。
    """
    import aiink.providers as providers_mod

    # 此处验证工具调用次数；共享 demo 的累积记忆会先撞到 token 预算，掩盖本测试目标。
    project_id = temp_project
    stub = ToolBudgetStub("金丹", "金丹")
    monkeypatch.setattr(providers_mod, "default_provider", stub)
    _reset_runs(project_id)

    graph = build_chapter_graph()
    thread = str(uuid.uuid4())
    result = graph.invoke(
        {"project_id": project_id, "chapter_seq": 1, "task_id": thread},
        config={"configurable": {"thread_id": thread}},
    )
    assert result.get("error") is None, result.get("error")
    assert stub.audit_calls == 4, f"audit 应 4 轮（3 工具轮 + 1 强制最终轮），实际 {stub.audit_calls}"
    trace = result.get("tool_trace") or []
    assert len(trace) == 3, f"预算应封顶 3 次工具执行，实际 {len(trace)}"
    assert (result.get("audit_verdict") or {}).get("verdict") == "pass"
    assert result.get("persisted") is True


def test_tools_execute_readonly(project_id):
    """只读查证工具确定性执行（§10 单元级）：4 个工具返回结构化 JSON；
    未知人物 found:false、未知工具名 error 不抛（坏输入不崩批次，§6.12）。
    """
    import json as _json

    from aiink.workflow.tools import execute_tool

    pid = uuid.UUID(project_id)
    with tenant_session(project_id) as db:
        r = _json.loads(execute_tool(db, pid, "inspect_character", {"name": "林砚"}))
        assert r.get("found") is True and "realm_cap" in r, r
        r2 = _json.loads(execute_tool(db, pid, "inspect_character", {"name": "不存在的人物"}))
        assert r2.get("found") is False, r2
        r3 = _json.loads(execute_tool(db, pid, "no_such_tool", {}))
        assert "error" in r3, "未知工具应返回 error 不抛"
        for tool in ("inspect_foreshadows", "inspect_plot_threads", "inspect_facts"):
            r4 = _json.loads(execute_tool(db, pid, tool, {}))
            assert "error" not in r4, r4


def test_deepseek_tool_call_parsing(monkeypatch):
    """DeepSeekProvider 工具轮（§10）：tools 进请求、关思考、无 response_format；
    tool_calls 解析为 {id, name, arguments dict}（arguments JSON 字符串容错为 {}）。
    """
    from aiink.providers.deepseek import DeepSeekProvider

    class FakeFn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class FakeTC:
        def __init__(self, id_, fn):
            self.id = id_
            self.function = fn

    class FakeMessage:
        def __init__(self, content="", tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class FakeUsage:
        prompt_tokens = 100
        completion_tokens = 50
        prompt_cache_hit_tokens = 0

    class FakeChoice:
        def __init__(self, message):
            self.message = message

    class FakeResp:
        def __init__(self, message):
            self.usage = FakeUsage()
            self.choices = [FakeChoice(message)]

    class FakeCompletions:
        def __init__(self):
            self.calls = []
            self.queue = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return self.queue.pop(0)

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self):
            self.chat = FakeChat()

    prov = DeepSeekProvider(api_key="test-key")
    client = FakeClient()
    monkeypatch.setattr(prov, "_client", client)

    # 工具轮：返回带 tool_calls 的响应（含一个坏 JSON arguments）
    client.chat.completions.queue.append(FakeResp(FakeMessage(
        tool_calls=[FakeTC("call_1", FakeFn("inspect_character", '{"name":"林砚"}')),
                    FakeTC("call_2", FakeFn("inspect_facts", "not-json"))],
    )))
    resp = prov.generate([{"role": "user", "content": "hi"}], model_id="deepseek-v4-flash",
                         tools=[{"type": "function"}])
    call = client.chat.completions.calls[-1]
    assert call["tools"] == [{"type": "function"}]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}, "工具轮应关思考"
    assert "response_format" not in call, "工具轮不应传 response_format"
    assert resp.tool_calls == [
        {"id": "call_1", "name": "inspect_character", "arguments": {"name": "林砚"}},
        {"id": "call_2", "name": "inspect_facts", "arguments": {}},  # 坏 JSON 容错为 {}
    ], resp.tool_calls

    # 无 tools + json_mode=True：保留现有行为（response_format 有、tools 无）
    client.chat.completions.queue.append(FakeResp(FakeMessage(content='{"v":1}')))
    resp2 = prov.generate([{"role": "user", "content": "hi"}], model_id="deepseek-v4-flash",
                          json_mode=True)
    call2 = client.chat.completions.calls[-1]
    assert call2["response_format"] == {"type": "json_object"}
    assert "tools" not in call2
    assert resp2.tool_calls is None

    # 纯文本正文（write/revise，§19.3 思考解耦）：disable_thinking 关思考、无 response_format
    client.chat.completions.queue.append(FakeResp(FakeMessage(content="正文")))
    resp3 = prov.generate([{"role": "user", "content": "hi"}], model_id="deepseek-v4-flash",
                          json_mode=False, disable_thinking=True)
    call3 = client.chat.completions.calls[-1]
    assert call3["extra_body"] == {"thinking": {"type": "disabled"}}, "纯文本正文应关思考"
    assert "response_format" not in call3, "非 json_mode 不应传 response_format"
    assert resp3.content == "正文"


def test_parse_json_tolerates_raw_control_chars():
    """§6.12 输出容错：DeepSeek 偶发 JSON 字符串里放原始换行 → 转义后仍能解析。"""
    from aiink.workflow.nodes import _parse_json

    # 原始换行字节（非 \n 两字符）是非法 JSON，应容错还原
    raw = '{"content": "第一段\n\n第二段"}'
    data = _parse_json(raw)
    assert data["content"].splitlines() == ["第一段", "", "第二段"]

    # 已合法转义的 \n 不应被误伤
    escaped = '{"content": "a\nb"}'
    assert _parse_json(escaped)["content"] == "a\nb"

    # 前/后杂质 + 控制字符组合仍能提取 {…} 子串
    messy = 'markdown ```\n{"content": "行1\n行2"}\n```'
    assert _parse_json(messy)["content"] == "行1\n行2"


def test_parse_json_preserves_structure_whitespace_after_escape():
    """§19.3 思考兜底必踩：多行 JSON 前导杂质时，结构空白不能被转义。

    旧 _escape_control_chars 用 [\\x00-\\x1f\\x7f] 无差别替换，把结构位置的合法换行
    转成 \\u000a 字面量 → 结构空白不接受转义序列 → Expecting property name char 1。
    2026-08-10 演示逼出：audit reasoning_content 兜底内容 = 前导思考文本 + 多行 JSON。
    """
    from aiink.workflow.nodes import _parse_json

    # 前导杂质 + 多行 JSON（含结构换行）+ 字符串值内 Unicode 行分隔符
    noisy = '我的判断：\n{\n  "verdict": "pass",\n  "reasons": ["第一点 第二点"]\n}\n补充'
    data = _parse_json(noisy)
    assert data["verdict"] == "pass"
    assert " " in data["reasons"][0]

    # 纯多行 JSON（无杂质）仍正常
    plain = '{\n  "verdict": "pass"\n}'
    assert _parse_json(plain)["verdict"] == "pass"


def test_parse_json_tolerates_trailing_junk_with_brace():
    """§6.12 输出容错：完整 JSON 后附含 } 的废话 → 只取首个完整对象。"""
    from aiink.workflow.nodes import _parse_json

    # 尾部杂质含 }（audit 实测形态），rfind("}") 截取会失败，raw_decode 只取首个对象
    noisy = '{"verdict":"pass","confidence":0.9}我认为可以。}结束'
    assert _parse_json(noisy)["verdict"] == "pass"

    # 前有语气词 + 尾部截断杂质
    messy = '好的，审核结果如下：{"verdict":"rewrite"} 后面还有{内容'
    assert _parse_json(messy)["verdict"] == "rewrite"


def test_parse_json_repairs_missing_commas_at_value_boundaries():
    """DeepSeek 偶发漏掉数组项/对象字段/对象项逗号；确定性修语法且保留原值。"""
    from aiink.workflow.nodes import _parse_json

    raw = '{"goals":["推进主线" "处理伤势"], "meta":{"a":1 "b":2}, "scenes":[{} {}]}'
    data = _parse_json(raw)

    assert data == {
        "goals": ["推进主线", "处理伤势"],
        "meta": {"a": 1, "b": 2},
        "scenes": [{}, {}],
    }


def test_parse_json_repairs_stray_quotes():
    """§6.12 输出容错：正文裸 ASCII 双引号（DeepSeek 偶发未转义）→ 修复后解析成功。"""
    from aiink.workflow.nodes import _parse_json

    # write 实测形态：content 正文里夹了未转义的 "（提前闭合 → Expecting ',' delimiter）
    raw = '{"content": "他说"好，这就来"就走", "title": "x"}'
    data = _parse_json(raw)
    assert data["content"] == '他说"好，这就来"就走'
    assert data["title"] == "x"

    # 连续两个裸引号（内容本身就是引号）也应正确还原
    double = '{"content": "他说:""", "t": "y"}'
    assert _parse_json(double)["content"] == '他说:""'


def test_parse_json_repair_noop_on_valid():
    """§6.12 输出容错：合法 JSON 走不到裸引号兜底，修复不改变解析结果。"""
    from aiink.workflow.nodes import _parse_json

    raw = '{"content": "正常文本，无引号", "title": "a"}'
    assert _parse_json(raw)["content"] == "正常文本，无引号"
    assert _parse_json(raw)["title"] == "a"


def test_parse_json_repairs_stray_quote_then_control_char():
    """§6.12 输出容错：裸引号提前闭合字符串后，后续原始控制字符会漏转义。

    真实 DeepSeek 输出（2026-08-17 破晓录 ch3 逼出）：plan_chapter JSON 字符串值内
    裸 ASCII 引号 + 原始换行组合 → _escape_control_chars 的 in_str 状态机被裸引号误导，
    控制字符停在「结构位置」漏转义 → Invalid control character。兜底顺序必须先修裸引号
    再转义控制字符（_escape_control_chars(_repair_stray_quotes(body))）。
    """
    from aiink.workflow.nodes import _parse_json

    raw = '{"content": "他说"好\n", "t": 1}'
    data = _parse_json(raw)
    assert data["content"] == "他说\"好\n"
    assert data["t"] == 1


def test_split_marked_no_marker_returns_default():
    """§6.12 标记锚点：模型漏写标记时整段归 default（兜底不丢章）。"""
    from aiink.workflow.nodes import _split_marked

    parts = _split_marked("林砚在黑市隐秘查探，玉佩气息若隐若现。")
    assert parts == {"default": "林砚在黑市隐秘查探，玉佩气息若隐若现。"}


def test_split_marked_single_marker_takes_block():
    """§6.12 标记锚点：=== CONTENT === 后文本归该键，标记前杂质归 default。"""
    from aiink.workflow.nodes import _split_marked

    parts = _split_marked("我先核实一下。\n=== CONTENT ===\n第一段\n\n第二段")
    assert parts.get("CONTENT") == "第一段\n\n第二段"
    assert parts.get("default") == "我先核实一下。"


def test_split_marked_multiple_markers_sections():
    """§6.12 标记锚点：多标记各归各键（revise 的 CONTENT + RESPONSES）。"""
    from aiink.workflow.nodes import _split_marked

    parts = _split_marked(
        "=== CONTENT ===\n修订后全文\n=== RESPONSES ===\n[{\"conflict_key\":\"a1\"}]"
    )
    assert parts.get("CONTENT") == "修订后全文"
    assert parts.get("RESPONSES") == '[{"conflict_key":"a1"}]'
    assert "default" not in parts


def test_split_marked_empty_block_after_marker():
    """§6.12 标记锚点：标记后空正文 → 该键为空字符串。"""
    from aiink.workflow.nodes import _split_marked

    parts = _split_marked("=== CONTENT ===\n")
    assert parts.get("CONTENT") == ""


def test_split_marked_lowercase_and_chinese_markers():
    """§6.12 标记锚点：小写/中文标记名不污染正文（归一到大写键 + 命名块）。"""
    from aiink.workflow.nodes import _split_marked

    parts = _split_marked("=== content ===\n第一段")
    assert parts.get("CONTENT") == "第一段", "小写标记应归一到大写键"
    parts2 = _split_marked("=== 正文 ===\n修订正文\n=== responses ===\n[]")
    assert parts2.get("正文") == "修订正文", "中文标记应成命名块而非归 default 污染正文"
    assert parts2.get("RESPONSES") == "[]", "小写 responses 应归一到大写键"


def test_content_block_prefers_content_then_named():
    """§6.12 标记锚点：_content_block 优先 CONTENT，错写标记名时取首个非空命名块。"""
    from aiink.workflow.nodes import _content_block

    assert _content_block({"CONTENT": "正文", "RESPONSES": "[]"}, "raw") == "正文"
    # 模型错写中文标记（无 CONTENT/default）→ 取首个非空命名块，标记行不落库
    assert _content_block({"正文": "修订全文", "RESPONSES": "[]"}, "raw") == "修订全文"
    assert _content_block({"default": "无标记正文"}, "raw") == "无标记正文"
    assert _content_block({}, "整段原文") == "整段原文"


def test_check_draft_strips_thinking_wrappers():
    from aiink.workflow.nodes import _check_draft

    assert _check_draft("<think>先列大纲</think>\n=== CONTENT ===\n林砚推门而出。") == "林砚推门而出。"
    assert _check_draft("<think>这段不该落库</think>\n夜色沉沉，林砚立在廊下。") == "夜色沉沉，林砚立在廊下。"


def test_looks_like_tool_noise_detects_text_tool_calls():
    """§10 write/revise 跑偏：模型把工具调用写成文本而非 function calling → 判噪声。"""
    from aiink.workflow.nodes import _looks_like_tool_noise

    noise = ('<ai_output>\n<function_results>\n<invoke name="inspect_facts">\n'
             '</function_results>\n</ai_output>')
    assert _looks_like_tool_noise(noise) is True
    assert _looks_like_tool_noise('<tool_use id="x">') is True
    assert _looks_like_tool_noise("夜色沉沉，林砚推门而出，玉佩在腰间轻震。") is False
    assert _looks_like_tool_noise("") is False


def test_coerce_str_lists_normalizes_llm_object_arrays():
    """§6.12 输出容错：list[str] 字段被 LLM 写成对象数组（{'hook': ...}）→ 归一为纯字符串列表。"""
    from aiink.schemas.contract import ChapterPlan
    from aiink.workflow.nodes import _coerce_str_lists

    data = _coerce_str_lists({
        "goals": [{"goal": "推动主线"}],
        "hooks_to_plant": [{"hook": "神秘玉佩"}],
        "hooks_to_resolve": ["旧伏笔"],
        "expected_events": [{"event": "宗门大比"}, {"event": "夺宝"}],
        "hard_constraints": [{"constraint": "不改设定"}, "不崩战力"],
        "unknown_key": [{"x": 1}],  # 非 ChapterPlan 字段：coerce 不碰，由调用方 filter
    }, ChapterPlan)

    assert data["goals"] == ["推动主线"]
    assert data["hooks_to_plant"] == ["神秘玉佩"]
    assert data["hooks_to_resolve"] == ["旧伏笔"]
    assert data["expected_events"] == ["宗门大比", "夺宝"]
    assert data["hard_constraints"] == ["不改设定", "不崩战力"]
    # 非 list[str] 字段原样保留
    assert data["unknown_key"] == [{"x": 1}]


def test_storage_indexes_exist_and_idempotent():
    """评审存储建议：组合索引 + HNSW 已声明且活库补齐（幂等重跑不报错）。

    - 组合索引前缀 = 真实查询热键（events 近章召回 / character_states 台账物化 /
      relations 双向 1-2 跳 / embeddings 按 project+level 过滤 / agent_runs 按 task_id 增量扫）；
    - HNSW 用 vector_cosine_ops，与 PgvectorStore.search 的 cosine_distance 对齐（§5.2/§14.1）。
    - agent_runs 无 RLS（观测表），查询按 task_id 前缀，索引前缀用 task_id 而非 project_id。
    """
    from sqlalchemy import text

    from aiink.db import ensure_storage_indexes, get_admin_engine

    _EXPECTED = {
        "ix_character_states_project_char_seq",
        "ix_events_project_chapter",
        "ix_relations_project_source",
        "ix_relations_project_target",
        "ix_embeddings_project_level",
        "ix_embeddings_embedding_hnsw",
        "ix_agent_runs_task_id",
    }
    # 幂等重跑（活库已建则应跳过，重跑不报错、不重复建）
    ensure_storage_indexes()
    ensure_storage_indexes()
    with get_admin_engine().connect() as c:
        rows = set(c.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
        )).scalars())
    missing = _EXPECTED - rows
    assert not missing, f"缺索引: {missing}"
    # HNSW 用 cosine 算子类（与 search 的 cosine_distance 对齐）
    with get_admin_engine().connect() as c:
        ddl = c.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname='ix_embeddings_embedding_hnsw'"
        )).scalar()
    assert "USING hnsw" in ddl and "vector_cosine_ops" in ddl, ddl
