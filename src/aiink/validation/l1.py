"""L1 确定性校验（§8.4）——无模型、纯规则、可复现。

作用于 extract 出的结构化候选（MutationCandidate）对比台账：
- 境界跳级 / 越界（realm_order + realm_cap，样例 10/1）
- 死而复生（alive 台账 vs 候选，样例 3）
- 战力通胀（per-角色 realm 序列斜率，样例 11，跨章）
- 候选 old_value-vs-台账（extract 误读注入快照，样例 16/20/21 确定性窄脚印）
- 关系台账自洽（重复/矛盾活跃行，§7.8）
- 伏笔烂尾 / 主线停滞（样例 18/19/23/26/27）
- 桥段重复向量近邻（样例 14，阴性 32 对照，§8.6）：事件向量近邻 + 呼应词豁免
- 高频句式统计（样例 15，§8.6 AI 味治理）：fatigue_words/patterns 频次超阈值 → style hint

conflict_key = hash(类型+实体+位置)，跨修订轮稳定（§6.4）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from aiink.memory import repository as repo
from aiink.memory.embedder import get_embedder
from aiink.memory.vector_store import PgvectorStore
from aiink.models import Event, Relation
from aiink.schemas import Finding, MutationCandidate

logger = logging.getLogger(__name__)

# 样例 11 阈值：连续 3 章内每次 chapter 都升境界 → 无铺垫通胀强信号
_INFLATION_WINDOW = 3
_INFLATION_STEPS = 2

# 样例 18/23 阈值：伏笔债务（planted 无回收条件 15 章无触碰 / developing 捡起后 10 章不落地 → 烂尾 hint）
_FS_PLANTED_STALL = 15
_FS_DEVELOPING_STALL = 10
# 样例 19 阈值：主线连续 15 章未推进 → 停滞 hint（支线可长期休眠，样例 27 阴性）
_THREAD_STALL = 15

# 样例 15 阈值：AI 味高频句式/用词统计（§8.6 文风与 AI 味治理检测侧）。
# 单句式模式单章 ≥2 次 / 单高频词单章 ≥3 次 → 记 offender。宁缺毋滥（§8.8）：
# 文风是作者自由，只暴露"复发趋势"标 hint 不阻塞。样例 15 单章内 2 处「不是…而是…」恰在边界。
_STYLE_PATTERN_CAP = 2
_STYLE_WORD_CAP = 3

# 样例 14/32 阈值：桥段重复（事件向量近邻，§8.6）。跨章最小间隔 10（样例 14 ch5→ch15 恰在边界）；
# cosine distance < 0.3 ⟺ 余弦相似度 > 0.7。宁缺毋滥（§8.8）：先保阴性 0 误报，再抬阳性检出率。
_REPEAT_MIN_GAP = 10
_REPEAT_DIST_THRESHOLD = 0.3
_REPEAT_TOP_K = 10
_REPEAT_QUERY_CAP = 3

# 呼应豁免词表（样例 32 阴性 0 误报关键）：正文/候选摘要含任一标记 → 整章/该候选豁免。
# 词表故意偏宽（宽豁免只损检出率不损误报率）；未覆盖表述靠 L2 抽样兜底（阶段 3 收尾）。
_CALLBACK_MARKERS = (
    "当年", "昔日", "想当年", "忆当年", "与当年",      # 时间回指
    "何其相似", "如出一辙", "似曾相识", "恍如隔世",      # 相似性元叙述
    "故技重施", "旧事重演", "历史重演", "重演",          # 明示重复
    "再现", "这一幕", "此情此景", "同样的一幕",          # 场景回指
    "忆起", "历历在目", "仿佛昨日",                      # 记忆唤起
)

# 样例 2/40 阈值：阵营敌对（§8.4 L1 第 1 类）。活跃 hostile 关系双名共现 + 任一协作标记 → 冲突。
# 词表故意偏窄（宁缺毋滥 §8.8）：只收明确协作语义的标记；且仅当 hostile 关系存在才触发，
# 双名须共现于本章 draft。合法联手须先落临时盟约（relation 带 valid_to）→ 守卫跳过（样例 40）。
_FACTION_COOP_MARKERS = (
    "并肩作战", "并肩而立", "共抗", "携手", "联手",
    "化敌为友", "握手言和", "把酒言和",
)

# 通用台账比对字段：realm/alive 已有 _realm_checks/_alive_checks 消费 old_value，
# 通用检查仅覆盖无专属检查的字段（防同候选双报 + test_flow 样例 1 类别级不回归）。
# 样例 16/20/21 的确定性窄脚印（状态机一致性）——正文-台账语义比对留 L2。
_GENERAL_STATE_FIELDS = ("location", "injury", "power", "item", "knowledge", "goal", "identity")


def _key(conflict_type: str, entity: str, chapter_seq: int) -> str:
    return hashlib.md5(f"{conflict_type}:{entity}:{chapter_seq}".encode()).hexdigest()[:16]


def _has_callback_marker(text: str | None) -> bool:
    """呼应豁免：文本含任一呼应/回指元叙述标记词（样例 32 阴性 0 误报）。"""
    return bool(text) and any(m in text for m in _CALLBACK_MARKERS)


def _names_present(draft: str, ch: object) -> bool:
    """角色名精确子串出现于 draft（样例 2 双名共现判据）。"""
    return ch.name in draft


def _temp_alliance_covers(session: Session, project_id: uuid.UUID,
                          source_id: uuid.UUID, target_id: uuid.UUID, chapter_seq: int) -> bool:
    """守卫（样例 40）：pair 存在非 hostile 关系行覆盖本章 → 合法联手/已非敌对，跳过。

    - 覆盖 = valid_from <= chapter_seq 且 (valid_to IS NULL 或 valid_to >= chapter_seq)；
    - 双向（(a,b) 与 (b,a) 都查）；含带 valid_to 的临时盟约与永久非敌对行——
      后者本会同有 hostile 行 → relation_ledger_check 判矛盾兜底，此处防双报。
    """
    rows = session.execute(
        select(Relation).where(
            Relation.project_id == project_id,
            or_(
                and_(Relation.source_id == source_id, Relation.target_id == target_id),
                and_(Relation.source_id == target_id, Relation.target_id == source_id),
            ),
            Relation.relation_type != "hostile",
            or_(Relation.valid_from.is_(None), Relation.valid_from <= chapter_seq),
            or_(Relation.valid_to.is_(None), Relation.valid_to >= chapter_seq),
        )
    ).scalars().all()
    return bool(rows)


class L1Validator:
    def __init__(self, realm_order: list[str]):
        self.realm_order = realm_order
        self.realm_pos = {r: i for i, r in enumerate(realm_order)}

    def validate(self, session: Session, *, project_id: uuid.UUID, chapter_seq: int,
                 candidates: list[MutationCandidate]) -> list[Finding]:
        findings: list[Finding] = []
        characters: dict[uuid.UUID, object] = {}

        for cand in candidates:
            if cand.kind == "relation_change":
                findings.extend(self._relation_change_ledger_check(
                    session, project_id, chapter_seq, cand))
                continue
            if cand.kind != "character_state":
                continue
            payload = cand.payload
            ch_id = payload.get("character_id")
            if ch_id is None:
                continue
            characters.setdefault(uuid.UUID(str(ch_id)), None)

            field = payload.get("field")
            old_v = payload.get("old_value")
            new_v = payload.get("new_value")

            if field == "realm" and new_v:
                findings.extend(self._realm_checks(session, project_id, chapter_seq,
                                                   str(ch_id), old_v, str(new_v), cand))
            elif field == "alive":
                findings.extend(self._alive_checks(session, project_id, chapter_seq,
                                                   str(ch_id), old_v, new_v, cand))
            elif field in _GENERAL_STATE_FIELDS:
                findings.extend(self._old_value_ledger_check(session, project_id, chapter_seq,
                                                             str(ch_id), field, old_v, cand))

        # 战力通胀（跨章序列，不依赖候选）
        findings.extend(self.power_inflation_check(session, project_id, chapter_seq))
        # 长线债务（伏笔烂尾 / 主线停滞，不依赖候选；hint 不阻塞，§8.6 线程债务 + §7.9 伏笔治理）
        findings.extend(self.foreshadow_debt_check(session, project_id, chapter_seq))
        findings.extend(self.plot_thread_debt_check(session, project_id, chapter_seq))
        # 关系台账自洽（仅活跃行；重复/矛盾 → major 不阻塞，§7.8 关系写入语义）
        findings.extend(self.relation_ledger_check(session, project_id, chapter_seq))
        return findings

    # ---- 伏笔烂尾（样例 18/23，阴性 26 对照，§7.9）----
    def foreshadow_debt_check(self, session: Session, project_id: uuid.UUID,
                              chapter_seq: int) -> list[Finding]:
        findings: list[Finding] = []
        for f in repo.get_open_foreshadows(session, project_id):  # planted / developing
            last = f.last_touched or f.planted_chapter
            if not last:
                continue
            if f.status == "developing":
                # 样例 23：作者已捡起（developing）却长期不落地 → 烂尾（不依赖 trigger 成熟度，
                # 以状态作"已承诺"的确定性代理；成熟度语义判断属 L2/阶段 3 主体）
                if last > chapter_seq - _FS_DEVELOPING_STALL:
                    continue
            else:
                # 样例 18：planted 且无回收条件（trigger 空）却长期无触碰 → 烂尾
                if f.trigger:
                    continue  # 有回收条件 = 刻意长沉（样例 26 阴性，0 误报）
                if last > chapter_seq - _FS_PLANTED_STALL:
                    continue
            findings.append(Finding(
                conflict_key=_key("foreshadow", str(f.id), chapter_seq),
                conflict_type="foreshadow", severity="hint", scope="local", source="L1",
                evidence=[{"chapter": chapter_seq,
                           "quote": f"伏笔「{f.description[:40]}」自第 {last} 章起 {chapter_seq - last} 章无触碰"}],
                suggestion="伏笔疑似烂尾：作者决策收/弃（标 resolved/dropped）或补剧情触碰（§7.9）",
            ))
        return findings

    # ---- 剧情线停滞（样例 19，阴性 27 对照，§8.6 P1 线程债务）----
    def plot_thread_debt_check(self, session: Session, project_id: uuid.UUID,
                               chapter_seq: int) -> list[Finding]:
        findings: list[Finding] = []
        for t in repo.get_plot_threads(session, project_id):  # status=active
            if t.kind != "main":
                continue  # 支线可长期休眠（样例 27 阴性）
            if not t.last_progress_chapter or t.last_progress_chapter > chapter_seq - _THREAD_STALL:
                continue
            findings.append(Finding(
                conflict_key=_key("plotline", str(t.id), chapter_seq),
                conflict_type="plotline", severity="hint", scope="local", source="L1",
                evidence=[{"chapter": chapter_seq,
                           "quote": f"主线「{t.name[:40]}」自第 {t.last_progress_chapter} 章起 "
                                    f"{chapter_seq - t.last_progress_chapter} 章未推进"}],
                suggestion="主线长期停滞：推进该线，或作者决策收线/降级（§8.6 P1）",
            ))
        return findings

    # ---- 关系台账自洽（样例 17/22 台账侧 + 新增样例，§7.8）----
    def relation_ledger_check(self, session: Session, project_id: uuid.UUID,
                              chapter_seq: int) -> list[Finding]:
        """活跃关系台账自洽（仅 valid_to IS NULL）：

        - 重复活跃：同 (source_id, target_id, relation_type) ≥2 条活跃 → 当前关系不可判定；
        - 矛盾活跃：同 (source_id, target_id) 存在 ≥2 种不同 relation_type 活跃 → 敌/盟并存。
        纯结构不变量（非阈值/语义猜测），合法数据 0 误报：persist 关闭修复保证新写入不产生；
        demo 关系表为空默认不触发。major/structural：报告 + 进 revise 上下文，不阻塞（§6.12）。
        """
        findings: list[Finding] = []
        relations = repo.get_relations(session, project_id)
        if not relations:
            return findings  # 空表廉价跳过
        by_pair: dict[tuple[uuid.UUID, uuid.UUID], list] = {}
        for r in relations:
            by_pair.setdefault((r.source_id, r.target_id), []).append(r)
        for (src, tgt), rows in by_pair.items():
            by_type: dict[str, list] = {}
            for r in rows:
                by_type.setdefault(r.relation_type, []).append(r)
            for rtype, typed in by_type.items():  # 重复活跃
                if len(typed) < 2:
                    continue
                chs = ",".join(str(r.source_chapter) for r in typed)
                findings.append(Finding(
                    conflict_key=_key("relation", f"dup:{src}:{tgt}", chapter_seq),
                    conflict_type="relation", severity="major", scope="structural", source="L1",
                    evidence=[{"chapter": chapter_seq,
                               "quote": f"关系 {src}→{tgt} 存在 {len(typed)} 条活跃 {rtype}（第 {chs} 章写入），当前关系不可判定"}],
                    suggestion="台账重复活跃：合并/关闭至一条（persist 关闭修复已防新增，存量人工收口）",
                ))
            if len(by_type) >= 2:  # 矛盾活跃
                kinds = " + ".join(f"{t}×{len(r)}" for t, r in by_type.items())
                findings.append(Finding(
                    conflict_key=_key("relation", f"con:{src}:{tgt}", chapter_seq),
                    conflict_type="relation", severity="major", scope="structural", source="L1",
                    evidence=[{"chapter": chapter_seq,
                               "quote": f"关系 {src}→{tgt} 同时活跃 {kinds}，敌/盟语义互相矛盾"}],
                    suggestion="台账矛盾活跃：确定当前唯一关系类型并关闭其余行（§7.8）",
                ))
        return findings

    # ---- 桥段重复向量近邻（样例 14，阴性 32 对照，§8.6；draft 依赖故由 service 编排）----
    def bridge_repeat_check(self, session: Session, *, project_id: uuid.UUID,
                            chapter_seq: int, candidates: list[MutationCandidate],
                            draft: str | None = None) -> list[Finding]:
        """当前章事件候选摘要 vs 历史事件向量近邻 → 疑似偷懒重复桥段。

        - 命中判据：cosine distance < _REPEAT_DIST_THRESHOLD 且历史事件章距 >= _REPEAT_MIN_GAP；
        - 呼应豁免（样例 32）：draft 或候选摘要含呼应标记词 → 整章/该候选跳过（宁缺毋滥）；
        - 每章至多 1 条 hint（style/local），append 后即出即止；
        - 加分项：embedder/向量不可用一律静默跳过（try/except 降级，不阻塞生成，§6.12）。
        """
        findings: list[Finding] = []
        try:
            if draft and _has_callback_marker(draft):
                return findings  # 本章含呼应意图 → 整章豁免（样例 32）
            ev_cands = [c for c in candidates
                        if c.kind == "event" and isinstance(c.payload, dict) and c.payload.get("summary")]
            if not ev_cands:
                return findings
            ev_cands = sorted(ev_cands, key=lambda c: c.confidence, reverse=True)[:_REPEAT_QUERY_CAP]
            vecs = get_embedder().encode([c.payload["summary"] for c in ev_cands])  # 一次批量前向
            for cand, emb in zip(ev_cands, vecs):
                summary = cand.payload["summary"]
                if _has_callback_marker(summary):
                    continue  # 候选摘要自带呼应意图
                hits = PgvectorStore().search(session, project_id=project_id, level="event",
                                              embedding=emb, top_k=_REPEAT_TOP_K)
                if not hits:
                    continue  # 无向量数据 → 跳过
                rows = session.execute(
                    select(Event).where(Event.id.in_([sid for sid, _ in hits]))
                ).scalars().all()
                by_id = {e.id: e for e in rows}
                for sid, dist in hits:
                    ev = by_id.get(sid)
                    if ev is None or ev.source_chapter > chapter_seq - _REPEAT_MIN_GAP:
                        continue  # 章距太近：正常情节连续性，非偷懒重复
                    if dist >= _REPEAT_DIST_THRESHOLD:
                        continue
                    findings.append(Finding(
                        conflict_key=_key("style", f"bridge:{str(sid)}", chapter_seq),
                        conflict_type="style", severity="hint", scope="local", source="L1",
                        evidence=[{"chapter": chapter_seq,
                                   "quote": f"本章事件「{summary[:40]}」与第 {ev.source_chapter} 章事件"
                                            f"「{ev.summary[:40]}」高度相似（余弦距离 {dist:.3f}）"}],
                        suggestion="疑似桥段偷懒重复：若为刻意呼应请补意图/差异，否则改写桥段（§8.6）",
                    ))
                    return findings  # 每章至多 1 条，即出即止（宁缺毋滥）
        except Exception as exc:
            logger.warning("桥段重复向量近邻失败，跳过（加分项不阻塞）: %s", exc)
        return findings

    # ---- 高频句式统计（样例 15，§8.6 文风与 AI 味治理检测侧；draft 依赖故由 service 编排）----
    def style_repeat_check(self, session: Session, *, project_id: uuid.UUID,
                           chapter_seq: int, draft: str | None = None) -> list[Finding]:
        """本章 draft 中 AI 味高频句式/用词频次统计，超阈值提示。

        - 判据：style_profile.fatigue_words（词级 draft.count）≥ _STYLE_WORD_CAP，
          fatigue_patterns（句式 regex re.findall）≥ _STYLE_PATTERN_CAP → 记 offender；
        - 任一 offender → 1 条 style/hint/local hint（每章至多 1 条，宁缺毋滥）；
        - 降级：无 draft / 无 fatigue 字段 / get_settings 异常 → 一律跳过不阻塞（§6.12）。
        """
        findings: list[Finding] = []
        try:
            if not draft:
                return findings
            settings = repo.get_settings(session, project_id)
            sp = settings.style_profile if settings else {}
            words = sp.get("fatigue_words") or []
            pats = sp.get("fatigue_patterns") or []
            if not words and not pats:
                return findings
            offenders: list[tuple[str, int]] = []
            for w in words:
                n = draft.count(w)
                if n >= _STYLE_WORD_CAP:
                    offenders.append((w, n))
            for p in pats:
                try:
                    n = len(re.findall(p, draft))
                except re.error:
                    continue  # 非法 regex 跳过该 pattern，不阻塞
                if n >= _STYLE_PATTERN_CAP:
                    offenders.append((p, n))
            if not offenders:
                return findings
            top = sorted(offenders, key=lambda x: x[1], reverse=True)[:3]
            offenders_str = "、".join(f"「{o}」×{n}" for o, n in top)
            findings.append(Finding(
                conflict_key=_key("style", "fatigue", chapter_seq),
                conflict_type="style", severity="hint", scope="local", source="L1",
                evidence=[{"chapter": chapter_seq,
                           "quote": f"本章高频句式/用词：{offenders_str}"
                                    f"（句式≥{_STYLE_PATTERN_CAP}次/词≥{_STYLE_WORD_CAP}次阈值）"}],
                suggestion="AI 味句式/高频词复发：改写或补差异化表达；写章 Prompt 已注入禁忌（§8.6）",
            ))
        except Exception as exc:
            logger.warning("高频句式统计失败，跳过（加分项不阻塞）: %s", exc)
        return findings

    # ---- realm ----
    def _realm_checks(self, session: Session, project_id: uuid.UUID, chapter_seq: int,
                      ch_id: str, old_v: str | None, new_v: str, cand: MutationCandidate) -> list[Finding]:
        findings: list[Finding] = []
        # ch_id 是 extract 归一化后的 canonical id（uuid str），按主键查
        from aiink.models import Character
        char = session.get(Character, uuid.UUID(ch_id))
        if char is None:
            return findings

        old_pos = self.realm_pos.get(old_v or "")
        new_pos = self.realm_pos.get(new_v)
        if new_pos is None:
            return findings  # 未知境界，规则不覆盖（未定义领域不阻塞，§7.11）

        cap_pos = self.realm_pos.get(char.realm_cap)
        evidence = [{"chapter": cand.source_chapter, "quote": f"{char.name} 境界 {old_v or '?'} → {new_v}"}]

        # 越界：超过境界上限（样例 1）
        if cap_pos is not None and new_pos > cap_pos:
            findings.append(Finding(
                conflict_key=_key("power", str(ch_id), chapter_seq),
                conflict_type="power", severity="critical", scope="structural", source="L1",
                evidence=evidence,
                suggestion=f"{char.name} 境界上限是 {char.realm_cap}，不能超过",
            ))
        # 跳级：跳过中间境界（样例 10）
        elif old_pos is not None and new_pos - old_pos > 1:
            findings.append(Finding(
                conflict_key=_key("power", str(ch_id), chapter_seq),
                conflict_type="power", severity="critical", scope="structural", source="L1",
                evidence=evidence,
                suggestion="境界需逐境晋升，不能跳级；需补突破契机说明",
            ))
        return findings

    # ---- alive ----
    def _alive_checks(self, session: Session, project_id: uuid.UUID, chapter_seq: int,
                      ch_id: str, old_v: str | None, new_v: object, cand: MutationCandidate) -> list[Finding]:
        findings: list[Finding] = []
        if new_v not in (True, "true", "alive", "生"):
            return findings  # 只拦"复活"方向

        # 台账该角色最近 alive 状态
        state = repo.get_character_state(session, project_id, uuid.UUID(ch_id), chapter_seq - 1)
        last = state.get("alive", "true").lower()
        if last in ("false", "dead", "死", "已死", "no"):
            findings.append(Finding(
                conflict_key=_key("character", str(ch_id), chapter_seq),
                conflict_type="character", severity="critical", scope="structural", source="L1",
                evidence=[{"chapter": cand.source_chapter, "quote": f"{ch_id} 台账已死却被写为存活"}],
                suggestion="死而复生需先落复活机制规则（facts），否则冲突",
            ))
        return findings

    # ---- 候选 old_value-vs-台账（样例 16/20/21 确定性窄脚印）----
    def _old_value_ledger_check(self, session: Session, project_id: uuid.UUID,
                                chapter_seq: int, ch_id: str, field: str, old_v: object,
                                cand: MutationCandidate) -> list[Finding]:
        """extract 候选 old_value vs 台账当前值（至 chapter_seq-1）：非空且不符 → 疑似误读台账。

        只比非空 old_value；台账该字段当前值为空/缺席（repo.get_character_state 至 seq-1）
        → 跳过（首写不变量，fresh 书 0 误报）。正文-台账语义比对（"无过渡推翻"）留 L2。
        """
        findings: list[Finding] = []
        if not old_v:
            return findings
        ledger = repo.get_character_state(session, project_id, uuid.UUID(ch_id), chapter_seq - 1)
        current = (ledger.get(field) or "").strip()
        if not current:
            return findings  # 台账无当前值（首写）即跳过
        if str(old_v).strip() != current:
            findings.append(Finding(
                conflict_key=_key("character_state", f"{ch_id}:{field}", chapter_seq),
                conflict_type="character_state", severity="minor", scope="local", source="L1",
                evidence=[{"chapter": cand.source_chapter,
                           "quote": f"候选 old_value={old_v!r} 与台账当前 {field}={current!r} 不符（抽取疑似误读注入快照）"}],
                suggestion="extract 读到旧台账快照：核对 recall 注入的状态快照，或重新抽取该字段",
            ))
        return findings

    # ---- 候选 relation_change old_value-vs-台账（样例 17/22 确定性窄脚印）----
    def _relation_change_ledger_check(self, session: Session, project_id: uuid.UUID,
                                      chapter_seq: int, cand: MutationCandidate) -> list[Finding]:
        """候选 relation_change old_value vs 台账当前有序对类型：非空且不符 → 疑似误读台账。

        只比非空 old_value；台账该有序对无活跃行 / 有歧义（>1 活跃行，重复/矛盾交
        relation_ledger_check 兜底）→ 跳过（首写/歧义不误报）。old_value == 台账 → 跳过，
        正文-台账语义比对（"无变更却表现相反"）留 L2。
        """
        findings: list[Finding] = []
        p = cand.payload
        old_v = (p.get("old_value") or "").strip()
        if not old_v:
            return findings
        src = p.get("source_id")
        tgt = p.get("target_id")
        if not src or not tgt:
            return findings
        try:
            current = repo.get_relation_current_type(session, project_id,
                                                     uuid.UUID(str(src)), uuid.UUID(str(tgt)))
        except ValueError:
            return findings
        if not current:
            return findings  # 台账无活跃关系或歧义（None）→ 跳过
        if old_v != current:
            findings.append(Finding(
                conflict_key=_key("relation", f"{src}:{tgt}", chapter_seq),
                conflict_type="relation", severity="minor", scope="local", source="L1",
                evidence=[{"chapter": cand.source_chapter,
                           "quote": f"候选 old_value={old_v!r} 与台账当前关系 {current!r} 不符（抽取疑似误读注入快照）"}],
                suggestion="extract 读到旧台账快照：核对 recall 注入的关系快照，或重新抽取该关系变更",
            ))
        return findings

    # ---- 战力通胀（跨章，样例 11）----
    def power_inflation_check(self, session: Session, project_id: uuid.UUID,
                              chapter_seq: int) -> list[Finding]:
        findings: list[Finding] = []
        for char in repo.get_all_characters(session, project_id):
            # 该角色 realm 台账序列（近窗口）
            from aiink.models import CharacterState
            rows = session.query(CharacterState).filter(
                CharacterState.project_id == project_id,
                CharacterState.character_id == char.id,
                CharacterState.field == "realm",
                CharacterState.chapter_seq <= chapter_seq,
            ).order_by(CharacterState.chapter_seq.desc()).limit(_INFLATION_WINDOW).all()

            if len(rows) < _INFLATION_WINDOW:
                continue
            # 逆序 → 从早到晚
            seq = list(reversed([r.new_value or "" for r in rows]))
            positions = [self.realm_pos.get(r) for r in seq]
            if any(p is None for p in positions):
                continue
            diffs = [positions[i + 1] - positions[i] for i in range(len(positions) - 1)]
            # 连续窗口内每次跳级（样例 11：无铺垫连续通胀）
            if len(diffs) >= _INFLATION_STEPS and all(d >= 1 for d in diffs):
                findings.append(Finding(
                    conflict_key=_key("power", str(char.id), chapter_seq),
                    conflict_type="power", severity="major", scope="structural", source="L1",
                    evidence=[{"chapter": chapter_seq, "quote": f"{char.name} 连续 {len(seq)} 章每章升境界: {' → '.join(seq)}"}],
                    suggestion="战力通胀强信号：连续数章无铺垫升级；若为奇遇需补代价说明，否则降为待审计",
                ))
        return findings

    # ---- 阵营敌对（样例 2，阴性样例 40 守卫，§8.4 L1 第 1 类）----
    def faction_check(self, session: Session, *, project_id: uuid.UUID,
                      chapter_seq: int, draft: str | None = None) -> list[Finding]:
        """本章 draft 中活跃敌对关系双名共现 + 协作标记 → faction/critical/structural。

        - 命中判据：relation_type=hostile 的活跃关系对；双名（含别名）共现于 draft；任一协作标记在 draft；
        - 守卫（样例 40）：临时盟约覆盖本章（任一带 valid_to 的非 hostile 行 valid_from<=ch<=valid_to，
          或永久有效的非 hostile 行）→ 跳过——合法联手须先落 relation 带 valid_to（spec 误报控制）；
          永久非敌对/同对歧义交 relation_ledger_check 兜底，防双报；
        - 每章至多 1 条（宁缺毋滥 §8.8），即出即止；降级：无 draft/异常一律跳过不阻塞（§6.12）。
        """
        findings: list[Finding] = []
        try:
            if not draft:
                return findings
            chars = {c.id: c for c in repo.get_all_characters(session, project_id)}
            if not chars:
                return findings
            hostile = [r for r in repo.get_relations(session, project_id)
                       if r.relation_type == "hostile"]
            for r in hostile:
                a, b = chars.get(r.source_id), chars.get(r.target_id)
                if a is None or b is None:
                    continue
                if not _names_present(draft, a) or not _names_present(draft, b):
                    continue
                if not any(m in draft for m in _FACTION_COOP_MARKERS):
                    continue
                if _temp_alliance_covers(session, project_id, r.source_id, r.target_id, chapter_seq):
                    continue  # 合法联手已落临时盟约（样例 40）
                findings.append(Finding(
                    conflict_key=_key("faction", f"{r.source_id}:{r.target_id}", chapter_seq),
                    conflict_type="faction", severity="critical", scope="structural", source="L1",
                    evidence=[{"chapter": chapter_seq,
                               "quote": f"敌对关系（{a.name} ↔ {b.name}）本章并肩协作，无临时盟约记录"}],
                    suggestion="阵营敌我矛盾：敌对双方并肩协作须先在 relations 落临时盟约（带 valid_to），否则冲突（§8.4 第 1 类）",
                ))
                return findings  # 每章至多 1 条
        except Exception as exc:
            logger.warning("阵营敌对检查失败，跳过（加分项不阻塞）: %s", exc)
        return findings
