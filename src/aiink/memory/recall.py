"""分层召回（recall 节点，§7 三层记忆 + §7.4 召回预算）。

阶段 1 主链路（关系查询，防走偏的第一道防线）：
- 硬约束（恒在 Top-K）+ 最近事件 + 出场人物状态台账快照 + 上一章摘要/开头；
- 开放伏笔 + 活跃剧情线注入（§7.9：plan_chapter 据此决定收/延/弃，防伏笔烂尾）。

向量召回（§15 阶段 1「最小向量召回」）：bge-m3 对上一章摘要做语义近邻，
补充关键词召回漏掉的历史相似事件（呼应/重复判据输入）。加分项——失败降级
为纯关系召回，不阻塞生成（§6.12 数据层）。

混合召回（§7.2/§16，2026-08-12 落地）：事件级双路——向量腿（level=event）
+ 关键词腿（出场人物名对事件摘要 ILIKE），RRF 融合排序；两腿独立降级。
recall_stats 上报召回占比（§16 工程评测）+ 各腿状态（ok/disabled/failed/empty/
no_terms/enabled_but_empty）与 degrade_reason：默认 EMBED_ENABLED=0 下向量腿是
「关闭」而不是「故障」，且「开关打开但索引为空」这一静默空转必须能看出来。
事实读侧腿本切片不做（world 索引写侧已落地，读侧待 get_hard_facts 改按相似度排序后激活）。
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aiink.config import settings
from aiink.memory import repository as repo
from aiink.memory.embedder import get_embedder
from aiink.memory.vector_store import PgvectorStore
from aiink.models import Chapter, Character, EmbeddingRow, Event, MemoryCandidate
from aiink.schemas import RetrievedContext
from aiink.context_budget import estimate_tokens
from aiink.validation.continuity import opening_excerpt

logger = logging.getLogger(__name__)


def _relation_target_name(session: Session, cid) -> str:
    """关系目标角色名（快照渲染用；角色已删则回退 id 字符串）。"""
    ch = session.get(Character, cid)
    return ch.name if ch else str(cid)




# 出场人物上限（recall 预算控制，防上下文膨胀）
_MAX_ENTITIES = 12
# 事件语义召回补充上限（去重后）
_VECTOR_RECALL_TOP_K = 5
# 关键词腿每级上限（§7.2：ILIKE 走不了索引，LIMIT 封顶防全表撑爆）
_KEYWORD_RECALL_TOP_K = 5
# 事件混合召回新增上限（去重后，防撑爆 _CONTENT_CAP 预算；recent 10 + 新增 ≤5 → mid ≤15）
_EVENT_RECALL_EXTRA_CAP = 5
# RRF 融合常数（Cormack et al. 标准值；腿内候选 ≤5 时排序主要由「命中几条腿」决定）
_RRF_K = 60
# 中文 1 字 ≈ 1.4 token（与 prompts.py §7.12 口径一致；token 估算不引 tokenizer 依赖）
_TOKEN_PER_CHAR = 1.4
# 硬约束/事件内容注入上限（§7.4 召回预算：防超长文本撑爆 12k tokens）
_CONTENT_CAP = 300
# 写作经验注入上限（§8.9 reflexion：8 条短经验 ≈ 300-500 tokens，兼容召回预算）
_MAX_LESSONS = 8
# 关键词腿术语上限（人物名去重后）
_MAX_KEYWORD_TERMS = 12
# 上一章结尾片段上限（字）：只够本章接续落点，不把全文喂进上下文（§11 防开头雷同）
_TAIL_CAP = 800


def _tail_of(content: str | None) -> str:
    """取正文结尾片段，对齐段落边界避免中途截断句子。

    content 是带自然换行的纯文本散文：取最后 _TAIL_CAP 字后，若窗口内出现段落换行
    则从最后一段开头取（给模型完整段落收尾）；窗口内无换行（单段落超长）则原样返回。
    """
    if not content:
        return ""
    text = content.strip()
    if len(text) <= _TAIL_CAP:
        return text  # 未截断时首段就是完整证据，不能因换行将其丢掉。
    tail = text[-_TAIL_CAP:]
    idx = tail.find("\n")
    if 0 < idx <= 80:
        tail = tail[idx + 1:].lstrip()
    return tail


def _merge_settings_constraints(session: Session, project_id: uuid.UUID,
                                facts_out: list[dict]) -> None:
    """project_settings.hard_constraints 并入硬约束列表（§7.11：设定是活数据）。

    facts 表是硬约束权威；project_settings.hard_constraints 是建书时的配置层约束
    （seed 双源）。合并按 content 去重：九州问天 同源只出现一次，示例书的
    题材硬约束（如「不得引入仙佛鬼神」）自此真正注入生成上下文。
    """
    settings_row = repo.get_settings(session, project_id)
    if not settings_row or not settings_row.hard_constraints:
        return
    seen = {f.get("content") for f in facts_out}
    for text in settings_row.hard_constraints:
        text = str(text or "").strip()
        if text and text not in seen:
            facts_out.append({"content": text, "source_chapter": None,
                              "category": "规则", "is_hard": True, "source": "project_settings"})
            seen.add(text)


def build_context(session: Session, *, project_id: uuid.UUID, chapter_seq: int,
                  participants: list[str] | None = None,
                  user_instruction: str | None = None,
                  shared_context: dict | None = None) -> RetrievedContext:
    """组装 recall 输出：设定 + 前情 + 状态快照 + 伏笔/剧情线 + 事件语义召回。

    批次级共享池（§6.11）：shared_context 携带批次内已组装一次的**稳定部分**
    （长期事实 hard_facts），传入则复用、不再查库；首章未缓存则查库并把结果回填
    到同一 dict，供批次层桥给后续章。变化部分（近期事件/伏笔/剧情线/快照）每章照查。
    """
    # 批次级共享池（§6.11）：注意仅当 None 才新建——调用方可能传入空 dict 期望原地回填
    # hard_facts（falsy 用 `or {}` 会重绑定新对象，回填丢失 → 后续章无法复用缓存）。
    if shared_context is None:
        shared_context = {}
    if "hard_facts" in shared_context:
        facts_out = shared_context["hard_facts"]  # 复用批次内已组装结果（§6.11）
    else:
        hard_facts = repo.get_hard_facts(session, project_id, chapter_seq)
        # content 必须带上：硬约束恒在 Top-K（§7.2），注入的是可读文本而非裸 id
        #（裸 id 模型不可反查 → 硬约束对生成实际不可见）。硬约束保留全文，由提示词总预算检查。
        facts_out = [
            {"fact_id": str(f.id), "source_chapter": f.source_chapter,
             "content": (f.content or "") if f.is_hard else (f.content or "")[:300], "category": f.category, "is_hard": bool(f.is_hard)}
            for f in hard_facts
        ]
        # §7.11 设定是活数据：project_settings.hard_constraints 一并并入硬约束（按内容去重，
        # 避免与 facts 表同源重复）。示例书（长安夜行/星舰远征）的题材硬约束此前从未生效。
        _merge_settings_constraints(session, project_id, facts_out)
        shared_context["hard_facts"] = facts_out
    recent_events = repo.get_recent_events(session, project_id, limit=10, before_chapter=chapter_seq)

    # 上一章摘要 + 结尾片段（短期上下文）。摘要概括主线；结尾片段是本章接续锚点——
    # 精确取前一章：重写/待确认重试时，latest 可能是本章或未来章，不能据此丢失前情。
    short: list[dict] = []
    rejected = session.query(MemoryCandidate).filter(
        MemoryCandidate.project_id == project_id, MemoryCandidate.status == "rejected",
        MemoryCandidate.source_chapter <= chapter_seq).all()
    rejected = [r for r in rejected if (r.review or {}).get("mode") == "revise"]
    if rejected:
        from aiink.workflow.review import rejection_text
        short.append({"kind": "author_review", "text": rejection_text(rejected)})

    prev = repo.get_chapter(session, project_id, chapter_seq - 1) if chapter_seq > 1 else None
    if prev:
        short.append({"kind": "prev_chapter_summary", "chapter": prev.chapter_seq, "summary": prev.summary or ""})
        tail = _tail_of(prev.content)
        if tail:
            short.append({"kind": "prev_chapter_tail", "chapter": prev.chapter_seq, "tail": tail})

    # 开场仅作差异化参照，与必须接续的章尾明确分开。只取已确认的历史章，最近优先。
    history = session.execute(select(Chapter).where(
        Chapter.project_id == project_id, Chapter.chapter_seq < chapter_seq,
        Chapter.status == "confirmed", Chapter.content.is_not(None),
    ).order_by(Chapter.chapter_seq.desc()).limit(4)).scalars().all()
    openings = [{"chapter": ch.chapter_seq, "text": opening_excerpt(ch.content)}
                for ch in history if (ch.content or "").strip()]

    # 出场人物状态快照（含当前关系——L2 正文-台账语义比对与写前预防的台账侧输入，
    # _render_entity 一并注入 write/plan/audit/extract；只取 ch 为 source 的有序对活跃行）
    snapshots: list[dict] = []
    if participants:
        for name in participants[: _MAX_ENTITIES]:
            ch = repo.get_character(session, project_id, name)
            if not ch:
                continue
            state = repo.get_character_state(session, project_id, ch.id, chapter_seq)
            relations = [
                {"target": _relation_target_name(session, r.target_id),
                 "relation_type": r.relation_type, "source_chapter": r.source_chapter}
                for r in repo.get_relations(session, project_id, entity_ids=[ch.id])
                if r.source_id == ch.id
            ]
            snapshots.append({
                "character_id": str(ch.id), "name": ch.name, "realm_cap": ch.realm_cap,
                "state": state, "personality": ch.personality, "relations": relations,
            })

    # 开放伏笔 + 活跃剧情线（§7.9 防伏笔烂尾：plan_chapter 输入，决定收/延/弃）
    foreshadows_out = [
        {"foreshadow_id": str(f.id), "description": f.description, "trigger": f.trigger,
         "planted_chapter": f.planted_chapter, "status": f.status}
        for f in repo.get_open_foreshadows(session, project_id)
    ]
    threads_out = [
        {"name": t.name, "kind": t.kind, "status": t.status, "progress": t.progress,
         "last_progress_chapter": t.last_progress_chapter}
        for t in repo.get_plot_threads(session, project_id)
    ]
    # 本书写作经验（§8.9 reflexion 注入）：在效经验 → 后续章规划/写作遵守；cap 上限
    # 兼容召回预算（8 条短经验 ≈ 300-500 tokens，远低于 §7.4 12k 预算）。
    lessons_out = [
        {"content": l.content, "lesson_type": l.lesson_type,
         "category": l.category, "source_chapter": l.source_chapter}
        for l in repo.get_active_lessons(session, project_id)[: _MAX_LESSONS]
    ]

    events_out = [
        {"event_id": str(e.id), "chapter": e.source_chapter, "confidence": e.confidence,
         "summary": (e.summary or "")[: _CONTENT_CAP]} for e in recent_events
    ]

    # 事件语义近邻召回（§15 最小向量召回）：以上一章摘要为 query，召回历史相似事件
    # ——补充关键词命中漏掉的呼应/重复素材；失败降级（模型未装/加载失败都不阻断）。
    if prev and prev.summary:
        recall_stats = _hybrid_recall(session, project_id, prev.summary, participants,
                                      events_out, facts_out)
    else:
        # 第 1 章没有前章摘要可作 query，且 before_chapter=1 让近期事件同样为空——这是设计
        # 如此（靠世界观 + 硬约束 + 大纲开场）。显式标注，便于区分「按设计没有」与「通道瘫了」。
        recall_stats = {"hybrid_status": "skipped_no_prev"}

    # 混合召回也可能返回本章旧版/未来事件；不能把这些当作已经发生的前情。
    events_out = [e for e in events_out if e["chapter"] < chapter_seq]
    ctx = RetrievedContext(
        long_term_facts=facts_out,
        mid_term_events=events_out,
        short_context=short,
        recent_openings=openings,
        entity_snapshots=snapshots,
        open_foreshadows=foreshadows_out,
        plot_threads=threads_out,
        reflexions=lessons_out,
    )
    if user_instruction:
        ctx.short_context.append({"kind": "user_instruction", "text": user_instruction})
    # 记录召回数据的估算量；提示词组装时按完整消息预算选择可选记忆。
    ctx.token_usage = estimate_tokens(ctx.model_dump(exclude={"token_usage", "recall_stats"}))
    ctx.recall_stats = recall_stats  # §16 召回占比 + 各腿状态（无前章 → skipped_no_prev）
    return ctx


def _rrf_fuse(legs: list[list[uuid.UUID]], k: int = _RRF_K) -> list[tuple[uuid.UUID, float]]:
    """RRF 融合（§7.2）：每条腿是**已去重、已按腿内质量排序**的 source_id 列表（rank 从 1 起）。

    score(id) = Σ_legs 1/(k + rank_leg(id))；返回 (source_id, score) 按 score 降序。
    空腿贡献 0；同一 id 出现在多条腿 → 跨腿加分（「向量+关键词双命中」权重更高）。
    """
    scores: dict[uuid.UUID, float] = {}
    for leg in legs:
        for rank, sid in enumerate(leg, start=1):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _esc_like(s: str) -> str:
    """ILIKE 模式转义（%、_、\\ 是通配符，命中即精确匹配术语子串）。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _terms(participants: list[str] | None) -> list[str]:
    """关键词腿术语：出场人物名（≥2 字符、去重、cap 12）。None/空 → []（腿空降级）。"""
    if not participants:
        return []
    out: list[str] = []
    for name in participants:
        name = str(name or "").strip()
        if len(name) < 2 or name in out:
            continue
        out.append(name)
        if len(out) >= _MAX_KEYWORD_TERMS:
            break
    return out


def _keyword_event_leg(session: Session, project_id: uuid.UUID,
                       terms: list[str]) -> list[uuid.UUID]:
    """关键词腿：事件摘要 ILIKE 命中任一人物名，近期优先。ILIKE 走不了 btree，
    但显式 project_id 过滤 + LIMIT 封顶（§14.1 坑 1 / MVP 量级可接受，pg_trgm 列后续）。"""
    if not terms:
        return []
    patterns = [f"%{_esc_like(t)}%" for t in terms]
    rows = session.execute(
        select(Event).where(
            Event.project_id == project_id,
            or_(*(Event.summary.ilike(p, escape="\\") for p in patterns)),
        ).order_by(Event.source_chapter.desc()).limit(_KEYWORD_RECALL_TOP_K)
    ).scalars().all()
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for e in rows:
        if e.id not in seen:
            seen.add(e.id)
            out.append(e.id)
    return out


def _has_event_embeddings(session: Session, project_id: uuid.UUID) -> bool:
    """本书是否已建事件向量索引。仅在向量检索 0 命中时调用，用于区分「确实没有相似
    事件」与「开关打开但索引从未建立」——后者是最初的静默空转状态，必须能看出来。"""
    return session.execute(
        select(EmbeddingRow.id).where(
            EmbeddingRow.project_id == project_id, EmbeddingRow.level == "event",
        ).limit(1)
    ).first() is not None


def _est_tokens(text: str) -> int:
    """中文 token 估算（1 字 ≈ 1.4 token，§7.12 口径；不引 tokenizer 依赖）。"""
    return int(len(text or "") * _TOKEN_PER_CHAR)


def _hybrid_recall(session: Session, project_id: uuid.UUID, query_text: str,
                   participants: list[str] | None,
                   events_out: list[dict], facts_out: list[dict]) -> dict:
    """事件混合召回（§7.2/§16）：向量腿（level=event）+ 关键词腿（人物名 ILIKE），RRF 融合。

    返回 recall_stats（§16 召回 token 占比 + 各腿运行状态）。任一条腿失败单独降级；
    双腿都没产出排序输入 → events_out 原样（纯关系召回兜底），但**仍返回带状态的
    stats**，调用方据此区分「确实没有相关事件」与「整条召回通道没跑起来」。
    **facts_out 只读**（供占比估算，不 mutate——防 shared_context 批次缓存污染）。
    """
    legs: list[tuple[str, list[uuid.UUID]]] = []
    tags: dict[uuid.UUID, set[str]] = {}
    vector_hits = keyword_hits = 0
    vector_status = "disabled"
    keyword_status = "no_terms"

    try:
        emb = get_embedder().encode([query_text])[0]
        hits = PgvectorStore().search(session, project_id=project_id, level="event",
                                      embedding=emb, top_k=_VECTOR_RECALL_TOP_K)
        vector_hits = len(hits)
        seen: set[uuid.UUID] = set()
        vec_ids: list[uuid.UUID] = []
        for sid, _ in hits:
            if sid not in seen:
                seen.add(sid)
                vec_ids.append(sid)
        if vec_ids:
            legs.append(("vector", vec_ids))
            for sid in vec_ids:
                tags.setdefault(sid, set()).add("vector")
            vector_status = "ok"
        else:
            # 0 命中分两种：确实没有相似事件（empty），或开关打开却从未建过索引
            # （enabled_but_empty）——后者是最初的静默空转，必须能在 log/stats 里看出来。
            vector_status = "empty" if _has_event_embeddings(session, project_id) else "enabled_but_empty"
            if vector_status == "enabled_but_empty":
                logger.warning("事件向量召回已启用但索引为空，本次退化为纯关键词召回（需先回填）")
    except Exception as exc:
        # EMBED_ENABLED=0 时 _DisabledEmbedder 必抛：这是既定的关闭路径，不是故障。
        # 两者分开记，否则默认配置下每次召回都会刷一条看起来像错误的 warning。
        if settings.embed_enabled:
            vector_status = "failed"
            logger.warning("向量腿召回失败，降级为关键词腿/纯关系: %s", exc)
        else:
            vector_status = "disabled"
            logger.info("向量召回已禁用（EMBED_ENABLED=0），本次按关键词腿/纯关系降级运行")

    try:
        terms = _terms(participants)
        if not terms:
            keyword_status = "no_terms"
        else:
            kw_ids = _keyword_event_leg(session, project_id, terms)
            keyword_hits = len(kw_ids)
            if kw_ids:
                legs.append(("keyword", kw_ids))
                for sid in kw_ids:
                    tags.setdefault(sid, set()).add("keyword")
                keyword_status = "ok"
            else:
                keyword_status = "empty"
    except Exception as exc:
        keyword_status = "failed"
        logger.warning("关键词腿召回失败，降级为向量腿/纯关系: %s", exc)

    added = 0
    if legs:
        fused = _rrf_fuse([ids for _, ids in legs])
        known = {str(e["event_id"]) for e in events_out}
        fused_ids = [sid for sid, _ in fused]
        rows = session.execute(
            select(Event).where(Event.id.in_(fused_ids))
        ).scalars().all()
        by_id = {str(e.id): e for e in rows}

        for sid, _ in fused:
            if added >= _EVENT_RECALL_EXTRA_CAP:
                break
            skey = str(sid)
            if skey in known or skey not in by_id:
                continue
            ev = by_id[skey]
            events_out.append({"event_id": skey, "chapter": ev.source_chapter,
                               "confidence": ev.confidence,
                               "recalled_by": "+".join(sorted(tags.get(sid, {"vector"}))),
                               "summary": (ev.summary or "")[:_CONTENT_CAP]})
            known.add(skey)
            added += 1
        logger.info("事件混合召回补充 %d 条（向量 %d / 关键词 %d）", added, vector_hits, keyword_hits)

    recall_tokens = sum(_est_tokens(e.get("summary", "")) for e in events_out if e.get("recalled_by"))
    context_tokens = (
        sum(_est_tokens(f.get("content", "")[: _CONTENT_CAP]) for f in facts_out)
        + sum(_est_tokens(e.get("summary", "")) for e in events_out)
    )
    # 降级原因：腿没跑成/没命中就记下来，调用方据此区分「按设计没有」与「通道瘫了」。
    # 各腿独立降级，任一条腿正常就不算全瘫，所以逐腿拼接而非只报一个总状态。
    reasons = [f"vector_{vector_status}"] if vector_status != "ok" else []
    if keyword_status != "ok":
        reasons.append(f"keyword_{keyword_status}")
    return {
        "vector_hits": vector_hits,
        "keyword_hits": keyword_hits,
        "fused_total": added,
        "recall_tokens_est": recall_tokens,
        "context_tokens_est": context_tokens,
        "share": round(recall_tokens / max(context_tokens, 1), 4),
        "vector_status": vector_status,
        "keyword_status": keyword_status,
        "degrade_reason": "+".join(reasons) if reasons else None,
    }
