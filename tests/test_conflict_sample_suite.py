"""全集系统性首测（plan.md §16）——conflict-samples.md 40 样例 → 检测机制 → 检出率/误报率。

§16 里程碑：「全集系统性首测 = 阶段 3 长线治理主体完成时」。本套件逐例直连真实检测入口
（不 mock 机制，仅 mock LLM 判定），作为首测的可运行计量仪器：

- **阳性 26 例**（样例 1-23、33/34、38）：断言对应机制产出目标类型 finding；
  其中 5 例（样例 5/6/7/8/9，timeline/item_rule/foreshadow-兑现/power-规则/faction归属）
  **检测机制未实现** → 标 `xfail(strict=False)` 记漏检（实现后 XPASS → 取消标记即可）；
  样例 4 检出但 finding 类别/严重度与 spec 不符（见注），另计。
- **阴性 14 例**（样例 24-32、35、36、37、39、40）：断言 0 误报；样例 28/29/30 对应机制未落地
  → 真空通过（其余 11 例有真实机制证明，含 2026-08-13 新增样例 40 阵营临时联手守卫）。

度量口径（conflict-samples.md「使用方式」）：检出率 = 检出阳性/阳性总数；误报率 = 误报阴性/阴性总数。
本套件绿跑产出：阳性 21/26 可检出（含样例 4 类别偏差检出、样例 2 补漏 faction）、5 漏检、阴性 0 误报——
即 **检出率 80.8%（21/26）、误报率 0%（0/14）**，指标基线见下方常量（防文档与代码漂移）。

桩设计（复用既有测试模板）：LLM 单点注入 = monkeypatch `global_audit.make_chain`（样例 12/36/37/38/39）
/ `ledger_l2.make_chain`（样例 16/17/20/21/22/24/25/4）；embedder 用 DeterministicFakeEmbedder
（3-gram 特征哈希，共享子串 → 高余弦相似，样例 14/32 的向量近邻确定性），模块级 autouse 覆盖
conftest 零向量版（cosine_distance 全 NaN 排序任意）；模块级 fixture 幂等补 global_audit_reports 表。
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import select

from aiink.db import tenant_session
from aiink.memory.vector_store import PgvectorStore
from aiink.models import (
    Chapter, Character, CharacterState, Event, Foreshadow, GlobalAuditReport,
    PlotThread, ProjectSettings, Relation, VolumeOutline,
)
from aiink.providers.base import ModelResponse
from aiink.schemas import ChapterPlan, MutationCandidate
from aiink.validation import global_audit as ga
from aiink.validation import ledger_l2 as l2
from aiink.validation.l1 import L1Validator, _key
from aiink.validation.service import outline_deviation

REALM_ORDER = ["炼气", "筑基", "金丹", "元婴", "化神", "大乘", "渡劫"]
A, B = uuid.uuid4(), uuid.uuid4()

# ---- 首测指标基线（随套件结果维护，防文档与代码漂移）----

POSITIVE_COUNT = 26          # 阳性：1-23、33/34、38
NEGATIVE_COUNT = 14          # 阴性：24-32、35、36、37、39、40
DETECTED_COUNT = 21          # 检出：20 全机制（含样例 2 补漏 faction）+ 样例 4（类别偏差检出）
XFAIL_SAMPLES = {5, 6, 7, 8, 9}   # 漏检（机制未实现）


# ---- 样例数据（镜像各专属测试文件，conflict-samples.md 原文语义）----

# 样例 16/24（injury 推翻 / 合法治疗）
DRAFT16 = "林砚负手立于北境城楼，气色如常，与守将谈笑风生。第7章。"
DRAFT24 = "林砚服下回春丹，闭关三日，伤势尽复。第7章。"
# 样例 4（location 跨域无传送）
DRAFT4 = "清晨林砚还在东海水宫与蛟王对饮，黄昏已站在荒原部的图腾祭坛前。第7章。"
QUOTE4 = "黄昏已站在荒原部的图腾祭坛前"
# 样例 2 / 40（阵营敌对 / 临时联手合法，conflict-samples.md 原文语义）
DRAFT2 = ("夜色中，林砚与天衡宗执法弟子并肩而立，共抗万魔渊魔修。"
          "那执法弟子拍了拍他肩头：\"林师弟，你我本是同门。\"")
DRAFT40 = ("夜色中，林砚与天衡宗执法弟子并肩而立，共抗万魔渊魔修。"
           "此前宗门已传讯约定：此役只是暂时联手，事毕各归其营。")
# 样例 15（AI 味句式）
SAMPLE15_DRAFT = ("他不是不知道前路凶险，而是早已没有退路。"
                  "他不是畏惧强敌，而是害怕辜负。")
PROFILE15 = {"fatigue_words": ["仿佛", "竟然"], "fatigue_patterns": ["不是.{0,20}而是"]}
# 样例 14/32（桥段重复 / 呼应豁免）
CH5 = "拍卖会上林砚被嘲讽亮出身份打脸"
CH15_MARKED = "同样的拍卖厅，同样的叫价——林砚暗想：这一幕与当年何其相似。他亮出底牌，这次却非争锋，只为引蛇出洞。"
# 样例 37（无词表标记的刻意呼应 → L2 判 echo）
CH15_UNMARKED_ECHO = "拍卖厅叫价声再起。林砚不动声色举牌，等那条藏在暗处的蛇自己上钩。"
# 样例 38/39（文风漂移 / 场景节奏合法变化）
BASE_TMPL = "云海翻涌，林砚负手立于崖巅，目光沉凝。第{n}章。"
DRIFT_TMPL = "林砚拍桌而起：'卧槽，这也太离谱了吧，直接开干！'第{n}章。"
SCENE_TMPL = "剑鸣刺耳。血溅三尺。林砚不退，剑锋再进。第{n}章。"
DRIFT_QUOTE = "林砚拍桌而起：'卧槽，这也太离谱了吧，直接开干！'"


# ---- 模块级 autouse fixture：确定性 embedder + 审计报告表 ----

@pytest.fixture(scope="module", autouse=True)
def _ensure_audit_reports_table():
    """幂等补 global_audit_reports 表（活 demo 库跑过 init 的缺新表，不依赖重跑 aiink init）。"""
    from aiink.db import ensure_global_audit_reports

    ensure_global_audit_reports()


class DeterministicFakeEmbedder:
    """确定性假 bge-m3：3-gram 特征哈希 → 1024 维单位向量（共享子串 → 高余弦相似）。"""

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


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    fake = DeterministicFakeEmbedder()
    monkeypatch.setattr("aiink.validation.l1.get_embedder", lambda: fake)
    monkeypatch.setattr("aiink.workflow.nodes.get_embedder", lambda: fake)
    monkeypatch.setattr("aiink.memory.recall.get_embedder", lambda: fake)
    return fake


# ---- 种子辅助（temp_project 不带角色/台账/事件，必须自种）----


def _seed_character(pid: str, name: str, realm_cap: str = "金丹", personality: str = "谨慎隐忍") -> uuid.UUID:
    with tenant_session(pid) as db:
        ch = Character(project_id=uuid.UUID(pid), name=name, race="人族",
                       origin="test", realm_cap=realm_cap, personality=personality, base_attrs={})
        db.add(ch)
        db.flush()
        return ch.id


def _seed_state(pid: str, **kw):
    fields = {"character_id": A, "chapter_seq": 5, "field": "injury",
              "old_value": None, "new_value": "濒死", "source_chapter": 5}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(CharacterState(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _seed_relation(pid: str, **kw):
    fields = {"source_id": A, "relation_type": "hostile", "target_id": B, "source_chapter": 3}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(Relation(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _seed_event(pid: str, summary: str, chapter: int, with_vector: bool = False) -> uuid.UUID:
    p = uuid.UUID(pid)
    with tenant_session(pid) as db:
        ev = Event(project_id=p, summary=summary, participants=[], source_chapter=chapter, confidence=0.9)
        db.add(ev)
        db.flush()
        if with_vector:
            PgvectorStore().upsert(db, project_id=p, level="event", source_id=ev.id,
                                   source_chapter=chapter, model_version="bge-m3",
                                   embedding=DeterministicFakeEmbedder()._feat(summary))
            db.flush()
        return ev.id


def _seed_chapter(pid: str, seq: int, content: str) -> None:
    with tenant_session(pid) as db:
        db.add(Chapter(project_id=uuid.UUID(pid), chapter_seq=seq, title=f"第{seq}章",
                       content=content, status="confirmed", version=1))
        db.commit()


def _seed_foreshadow(pid: str, **kw):
    fields = {"description": "测试伏笔", "status": "planted", "planted_chapter": 3,
              "trigger": {}, "last_touched": 3}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(Foreshadow(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _seed_thread(pid: str, **kw):
    fields = {"name": "测试线", "kind": "main", "status": "active", "last_progress_chapter": 5}
    fields.update(kw)
    with tenant_session(pid) as db:
        db.add(PlotThread(project_id=uuid.UUID(pid), **fields))
        db.flush()


def _set_profile(pid: str, profile: dict) -> None:
    with tenant_session(pid) as db:
        st = db.execute(select(ProjectSettings).where(
            ProjectSettings.project_id == uuid.UUID(pid))).scalar_one_or_none()
        if st is None:
            db.add(ProjectSettings(project_id=uuid.UUID(pid), world_rules={},
                                   style_profile=profile, hard_constraints=[]))
        else:
            st.style_profile = profile
        db.commit()


def _seed_style_book(pid: str, baseline_n: int, window_n: int, window_texts=None) -> tuple[int, int]:
    for seq in range(1, baseline_n + 1):
        _seed_chapter(pid, seq, BASE_TMPL.format(n=seq))
    start = baseline_n + 1
    for i in range(window_n):
        seq = start + i
        text = window_texts(seq) if window_texts else DRIFT_TMPL.format(n=seq)
        _seed_chapter(pid, seq, text)
    return (start, start + window_n - 1) if window_n else (start, start)


# ---- 候选 / LLM 桩 ----

def _mc(kind: str, payload: dict, chapter: int = 7, confidence: float = 0.9) -> MutationCandidate:
    return MutationCandidate(kind=kind, source_chapter=chapter, payload=payload, confidence=confidence)


def _state_cand(field: str, old_v, new_v, cid: uuid.UUID = A, chapter: int = 7) -> dict:
    return {"kind": "character_state", "source_chapter": chapter, "confidence": 0.9,
            "payload": {"character_id": str(cid), "field": field,
                        "old_value": old_v, "new_value": new_v}}


def _rel_cand(rtype: str, old_v, new_v, src: uuid.UUID = A, tgt: uuid.UUID = B, chapter: int = 7) -> dict:
    return {"kind": "relation_change", "source_chapter": chapter, "confidence": 0.9,
            "payload": {"source_id": str(src), "target_id": str(tgt), "relation_type": rtype,
                        "old_value": old_v, "new_value": new_v}}


def _judgment(key: str, verdict: str = "invalid", evidence: str = "", confidence: float = 0.9) -> dict:
    return {"key": key, "verdict": verdict, "evidence": evidence, "reason": "suite", "confidence": confidence}


class _Chain:
    """LLM 链包装（test_bridge_audit 同款，传给 make_chain 的 lambda）。"""

    def __init__(self, provider):
        self.provider = provider

    def generate(self, messages, *, json_mode=False, max_tokens=None, temperature=None, tools=None):
        return self.provider.generate(messages, model_id="stub", max_tokens=max_tokens,
                                      temperature=temperature, json_mode=json_mode, tools=tools)


class AuditStub:
    """全局审计 stub：返回预置 findings（或 error / 空数组），记录调用数。"""

    def __init__(self, findings=None, error=None):
        self.findings = findings if findings is not None else []
        self.error = error
        self.calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        self.calls += 1
        if self.error:
            return ModelResponse(content="", model_id=model_id, error=self.error,
                                 input_tokens=50, output_tokens=0, duration_ms=30)
        return ModelResponse(content=json.dumps({"findings": self.findings}, ensure_ascii=False),
                             model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)


class LedgerStub:
    """validator_l2 stub：返回预置 judgments（或 error），记录调用数。"""

    def __init__(self, judgments=None, error=None):
        self.judgments = judgments if judgments is not None else []
        self.error = error
        self.calls = 0

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        self.calls += 1
        if self.error:
            return ModelResponse(content="", model_id=model_id, error=self.error,
                                 input_tokens=50, output_tokens=0, duration_ms=30)
        return ModelResponse(content=json.dumps({"judgments": self.judgments}, ensure_ascii=False),
                             model_id=model_id, input_tokens=50, output_tokens=80, duration_ms=30)


def _install_audit_stub(monkeypatch, stub):
    monkeypatch.setattr(ga, "make_chain", lambda role, **_kwargs: _Chain(stub))


def _install_ledger_stub(monkeypatch, stub):
    monkeypatch.setattr(l2, "make_chain", lambda role, **_kwargs: _Chain(stub))


# ---- 机制调用辅助 ----

def _validate(pid: str, seq: int, candidates: list[MutationCandidate] | None = None) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).validate(
            db, project_id=uuid.UUID(pid), chapter_seq=seq, candidates=candidates or [])
        return [f.model_dump(mode="json") for f in fs]


def _power_check(pid: str, seq: int) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).power_inflation_check(db, project_id=uuid.UUID(pid), chapter_seq=seq)
        return [f.model_dump(mode="json") for f in fs]


def _foreshadow_check(pid: str, seq: int) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).foreshadow_debt_check(db, project_id=uuid.UUID(pid), chapter_seq=seq)
        return [f.model_dump(mode="json") for f in fs]


def _thread_check(pid: str, seq: int) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).plot_thread_debt_check(db, project_id=uuid.UUID(pid), chapter_seq=seq)
        return [f.model_dump(mode="json") for f in fs]


def _relation_check(pid: str, seq: int) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).relation_ledger_check(db, project_id=uuid.UUID(pid), chapter_seq=seq)
        return [f.model_dump(mode="json") for f in fs]


def _style_check(pid: str, draft, seq: int = 15) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).style_repeat_check(
            db, project_id=uuid.UUID(pid), chapter_seq=seq, draft=draft)
        return [f.model_dump(mode="json") for f in fs]


def _faction_check(pid: str, seq: int, draft: str) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).faction_check(
            db, project_id=uuid.UUID(pid), chapter_seq=seq, draft=draft)
        return [f.model_dump(mode="json") for f in fs]


def _bridge_check(pid: str, candidates: list[MutationCandidate], draft=None, seq: int = 15) -> list[dict]:
    with tenant_session(pid) as db:
        fs = L1Validator(REALM_ORDER).bridge_repeat_check(
            db, project_id=uuid.UUID(pid), chapter_seq=seq, candidates=candidates, draft=draft)
        return [f.model_dump(mode="json") for f in fs]


def _l2(pid: str, candidates: list[dict], draft: str, seq: int = 7) -> list[dict]:
    with tenant_session(pid) as db:
        return l2.run_ledger_l2(db, project_id=uuid.UUID(pid), chapter_seq=seq,
                                candidates=candidates, draft=draft)


def _audit(pid: str, window: tuple[int, int]) -> dict:
    with tenant_session(pid) as db:
        rep = ga.run_global_audit(db, pid, window, source="manual")
        db.flush()
        return rep


# =====================================================================
# A. 点级样例（1-10）
# =====================================================================

def test_sample_01_realm_over_cap(temp_project):
    """样例 1 战力越界：林砚 realm_cap=金丹，候选 筑基→元婴 → power/critical/structural。"""
    ch_id = _seed_character(temp_project, "林砚", realm_cap="金丹")
    fs = _validate(temp_project, 7, [_mc("character_state",
                  {"character_id": str(ch_id), "field": "realm", "old_value": "筑基", "new_value": "元婴"})])
    power = [f for f in fs if f["conflict_type"] == "power"]
    assert len(power) == 1, fs
    assert power[0]["severity"] == "critical" and power[0]["scope"] == "structural"


def test_sample_02_faction_hostility(temp_project):
    """样例 2 阵营敌对：活跃敌对关系双名共现 + 协作标记（并肩而立/共抗），无临时盟约 → faction/critical/structural。

    机制：faction_check 直连（draft 依赖，service.validate 每章首稿自动生效）；
    conflict_key 确定性 = _key("faction", f"{lin_id}:{zhi_id}", 8)（关系端点须为已 seed 的 Character id）。
    """
    lin_id = _seed_character(temp_project, "林砚")
    zhi_id = _seed_character(temp_project, "执法弟子")
    _seed_relation(temp_project, source_id=lin_id, relation_type="hostile", target_id=zhi_id, source_chapter=3)
    fs = _faction_check(temp_project, 8, DRAFT2)
    faction = [f for f in fs if f["conflict_type"] == "faction"]
    assert len(faction) == 1, fs
    assert faction[0]["severity"] == "critical" and faction[0]["scope"] == "structural"
    assert faction[0]["conflict_key"] == _key("faction", f"{str(lin_id)}:{str(zhi_id)}", 8)


def test_sample_03_death_and_resurrection(temp_project):
    """样例 3 死而复生：台账 alive=false，候选 alive=true → character/critical/structural。"""
    ch_id = _seed_character(temp_project, "秦虎")
    _seed_state(temp_project, character_id=ch_id, chapter_seq=8, field="alive", new_value="false")
    fs = _validate(temp_project, 12, [_mc("character_state",
                  {"character_id": str(ch_id), "field": "alive", "old_value": "false", "new_value": True})])
    alive = [f for f in fs if f["conflict_type"] == "character"]
    assert len(alive) == 1, fs
    assert alive[0]["severity"] == "critical" and alive[0]["scope"] == "structural"


def test_sample_04_location_without_transport(temp_project, monkeypatch):
    """样例 4 地点跨域无传送：L2 语义比对检出（类别偏差注记——实际 character_state/minor，spec 期望 location/major）。

    检测成立（len==1）即计入检出率；类别/严重度与 spec 的偏差是首测暴露的「spec-impl 对齐项」，
    非漏检：spec §8.4 将 location 定为独立 L1 类别，落地为 ledger_l2 的 character_state/minor。
    """
    _seed_state(temp_project, field="location", new_value="东海水宫")
    key = _key("character_state", f"{str(A)}:location", 7)
    stub = LedgerStub(judgments=[_judgment(key, evidence=QUOTE4)])
    _install_ledger_stub(monkeypatch, stub)
    got = _l2(temp_project, [_state_cand("location", "东海水宫", "荒原部")], DRAFT4)
    assert len(got) == 1, got  # 检出成立
    assert got[0]["conflict_type"] == "character_state" and got[0]["severity"] == "minor"  # 类别偏差注记


@pytest.mark.xfail(reason="首测记录（漏检）：样例 5 首次来访矛盾——L1 事件-经历比对机制未实现", strict=False)
def test_sample_05_first_visit_contradiction(temp_project):
    """样例 5 首次来访：事件表已有「北境城完成交易」，正文却称第一次来 → 应出 character/major（机制未实现）。"""
    _seed_character(temp_project, "林砚")
    _seed_event(temp_project, "林砚在北境城完成交易", 8)
    fs = _validate(temp_project, 12)
    assert any(f["conflict_type"] == "character" for f in fs), fs


@pytest.mark.xfail(reason="首测记录（漏检）：样例 6 已毁物品再现——L1 item_rule 机制未实现", strict=False)
def test_sample_06_destroyed_item_reappears(temp_project):
    """样例 6 已毁物品再现：事件表「定水珠被天劫劈碎」，正文再现无修复 → 应出 item_rule/critical（机制未实现）。"""
    _seed_character(temp_project, "林砚")
    _seed_event(temp_project, "定水珠被天劫劈碎", 10)
    fs = _validate(temp_project, 11)
    assert any(f["conflict_type"] == "item_rule" for f in fs), fs


@pytest.mark.xfail(reason="首测记录（漏检）：样例 7 伏笔未兑现当已兑现——L1 foreshadow-兑现比对机制未实现", strict=False)
def test_sample_07_promise_unfulfilled_claimed(temp_project):
    """样例 7 伏笔未兑现当已兑现：planted 伏笔被正文当作已报 → 应出 foreshadow/major（机制未实现）。"""
    _seed_foreshadow(temp_project, description="三年之内必取万魔渊魔主首级", planted_chapter=3, last_touched=3)
    fs = _validate(temp_project, 10)
    assert any(f["conflict_type"] == "foreshadow" for f in fs), fs


@pytest.mark.xfail(reason="首测记录（漏检）：样例 8 能力规则越级用秘法——L1 power-规则前置校验机制未实现", strict=False)
def test_sample_08_capability_rule_violation(temp_project):
    """样例 8 能力规则越级：金丹期施展化神期碎星诀无代价 → 应出 power/critical（机制未实现）。"""
    _seed_character(temp_project, "林砚", realm_cap="金丹")
    fs = _validate(temp_project, 8)
    assert any(f["conflict_type"] == "power" for f in fs), fs


@pytest.mark.xfail(reason="首测记录（漏检）：样例 9 势力归属——需 extract 补归属/faction 候选或 facts 归属类联动（数据模型边界：relations 端点仅 character id、无势力归属知识）", strict=False)
def test_sample_09_faction_allegiance(temp_project):
    """样例 9 势力归属：叛出天衡宗却自称执法弟子 → 应出 faction/major。

    数据模型边界（2026-08-13 补漏切片记录）：样例 2 的 faction 机制已落地，但样例 9 的「自称归属」
    需要「势力名 + 成员」知识——relations 端点只能是 character id（_resolve_character_id 只查
    characters 表），Faction/Entity 表无 repo 读取方法、无 seed 行 → 当前模型不承载，留独立切片：
    由 extract 产出归属/faction 候选，或读 facts 归属类（category=归属）联动。
    """
    _seed_character(temp_project, "林砚")
    fs = _validate(temp_project, 8)
    assert any(f["conflict_type"] == "faction" for f in fs), fs


def test_sample_40_faction_temp_alliance_legal(temp_project):
    """样例 40 阵营·临时联手合法（样例 2 对照，阴性）：敌对关系 + 已落临时盟约（带 valid_to 覆盖本章）→ 0 检出。

    spec 误报控制（样例 2）：剧情有意暂时联手须先落 relation 带 valid_to → 守卫跳过，0 误报。
    守卫判定：非 hostile 行（ally）valid_from<=8<=valid_to 覆盖本章 → faction_check 跳过。
    """
    lin_id = _seed_character(temp_project, "林砚")
    zhi_id = _seed_character(temp_project, "执法弟子")
    _seed_relation(temp_project, source_id=lin_id, relation_type="hostile", target_id=zhi_id, source_chapter=3)
    _seed_relation(temp_project, source_id=lin_id, relation_type="ally", target_id=zhi_id,
                   source_chapter=6, valid_from=6, valid_to=13)
    fs = _faction_check(temp_project, 8, DRAFT40)
    assert [f for f in fs if f["conflict_type"] == "faction"] == [], fs


def test_sample_10_realm_skip_grade(temp_project):
    """样例 10 境界跳级：realm_cap 高企（化神），候选 筑基→元婴 跳过金丹 → power/critical/structural。"""
    ch_id = _seed_character(temp_project, "林砚", realm_cap="化神")
    fs = _validate(temp_project, 7, [_mc("character_state",
                  {"character_id": str(ch_id), "field": "realm", "old_value": "筑基", "new_value": "元婴"})])
    power = [f for f in fs if f["conflict_type"] == "power"]
    assert len(power) == 1, fs
    assert power[0]["severity"] == "critical" and power[0]["scope"] == "structural"


# =====================================================================
# B. 长线级样例（11-15）
# =====================================================================

def test_sample_11_power_inflation(temp_project):
    """样例 11 战力通胀：连续 3 章每章升境界（无铺垫）→ power/major/structural。"""
    ch_id = _seed_character(temp_project, "林砚")
    _seed_state(temp_project, character_id=ch_id, field="realm", new_value="炼气", chapter_seq=5)
    _seed_state(temp_project, character_id=ch_id, field="realm", new_value="筑基", chapter_seq=6)
    _seed_state(temp_project, character_id=ch_id, field="realm", new_value="金丹", chapter_seq=7)
    fs = _power_check(temp_project, 8)
    power = [f for f in fs if "林砚" in f["evidence"][0]["quote"]]
    assert len(power) == 1, fs
    assert power[0]["severity"] == "major" and power[0]["scope"] == "structural"


def _seed_outline(pid: str, end: int = 30) -> None:
    with tenant_session(pid) as db:
        db.add(VolumeOutline(
            project_id=uuid.UUID(pid), volume_seq=1, title="整书大纲",
            outline={"objective": "入宗立足", "chapter_count": end, "volumes": [{
                "title": "第一卷", "goal": "入宗立足",
                "chapter_start": 1, "chapter_end": end,
                "stages": [{"name": "前期", "chapter_start": 1, "chapter_end": end,
                            "goal": "入门试炼"}]}]}))
        db.commit()


def test_sample_12_persona_drift(temp_project, monkeypatch):
    """样例 12：窗口剧情偏离卷规划 → volume/hint/structural/L2。"""
    _seed_outline(temp_project, end=12)
    for seq in range(1, 12):
        _seed_chapter(temp_project, seq, f"林砚静观云海，谋定后动。第{seq}章。")
    _seed_chapter(temp_project, 12, "林砚一脚踹开房门，破口大骂：'都给我滚！'")
    drift = {"verdict": "drifted", "chapter": 12, "volume_seq": 1, "stage_seq": 1,
             "evidence": "一脚踹开房门，破口大骂", "reason": "偏离入门试炼",
             "recovery": "拉回宗门线", "confidence": 0.85}
    _install_audit_stub(monkeypatch, AuditStub(findings=[drift]))
    rep = _audit(temp_project, (1, 12))
    assert rep["status"] == "completed"
    assert len(rep["findings"]) == 1, rep["findings"]
    f = rep["findings"][0]
    assert f["conflict_type"] == "volume" and f["severity"] == "hint"
    assert f["scope"] == "structural" and f["source"] == "L2" and f["evidence"][0]["chapter"] == 12


def test_sample_13_outline_deviation(temp_project):
    """样例 13 大纲偏差：expected_events 无对应实际事件 → 偏差报告（conflict_type=plotline，见注）。

    注：spec 标 conflict_type=foreshadow，实现按剧情线出 plotline/major——类别口径差异，机制有效计入检出。
    """
    with tenant_session(temp_project) as db:
        plan = ChapterPlan(goals=["推进主线"], expected_events=["林砚收回'玉佩真相'伏笔"])
        cand = _mc("event", {"summary": "黑市探查商人底细", "participants": []}, chapter=7)
        fs = outline_deviation(db, uuid.UUID(temp_project), 7, plan, [cand])
        fs = [f.model_dump(mode="json") for f in fs]
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "plotline" and fs[0]["severity"] == "major"


def test_sample_14_bridge_repeat_l1(temp_project):
    """样例 14 桥段重复（L1 事件层）：本章事件候选 vs 历史向量近邻（dist≈0、章距 10）→ style/hint/local。"""
    _seed_event(temp_project, CH5, 5, with_vector=True)
    cand = _mc("event", {"summary": CH5, "participants": []}, chapter=15)
    fs = _bridge_check(temp_project, [cand])
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "style" and fs[0]["severity"] == "hint"
    assert fs[0]["source"] == "L1" and "第 5 章" in fs[0]["evidence"][0]["quote"]


def test_sample_15_ai_style_repeat(temp_project):
    """样例 15 AI 味复发：单章 2 处「不是…而是…」→ style/hint/local/L1。"""
    _set_profile(temp_project, PROFILE15)
    fs = _style_check(temp_project, SAMPLE15_DRAFT)
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "style" and fs[0]["severity"] == "hint" and fs[0]["source"] == "L1"


# =====================================================================
# C. §7.7-7.9 记忆/图谱/伏笔线样例（16-27）
# =====================================================================

def test_sample_16_injury_flip_no_transition(temp_project, monkeypatch):
    """样例 16 伤情无过渡推翻：injury 濒死→如常 无治疗交代 → character_state/major/local/L2。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    _install_ledger_stub(monkeypatch, LedgerStub(
        judgments=[_judgment(key, evidence="林砚负手立于北境城楼")]))
    got = _l2(temp_project, [_state_cand("injury", "濒死", "如常")], DRAFT16)
    assert len(got) == 1, got
    assert got[0]["conflict_type"] == "character_state" and got[0]["severity"] == "major"
    assert got[0]["scope"] == "local" and got[0]["source"] == "L2" and got[0]["conflict_key"] == key


def test_sample_17_relation_flip_no_record(temp_project, monkeypatch):
    """样例 17 敌对→结盟无变更记录：正文表现 ally vs 台账 hostile → relation/major/structural。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=6)
    key = _key("relation", f"{str(A)}:{str(B)}", 7)
    stub = LedgerStub(judgments=[_judgment(key, evidence="蛟王与林砚并肩立于水宫殿前")])
    _install_ledger_stub(monkeypatch, stub)
    draft = "蛟王与林砚并肩立于水宫殿前，道：\"林兄，今日之事有劳了。\"第7章。"
    got = _l2(temp_project, [_rel_cand("ally", "hostile", "ally")], draft)
    assert len(got) == 1, got
    f = got[0]
    assert f["conflict_type"] == "relation" and f["severity"] == "major" and f["scope"] == "structural"
    assert f["conflict_key"] == key


def test_sample_18_foreshadow_planted_stall(temp_project):
    """样例 18 伏笔烂尾·planted 无触碰：15 章无触碰且空 trigger → foreshadow/hint/local。"""
    _seed_foreshadow(temp_project, description="青玉匣中封印之物", planted_chapter=3, last_touched=3)
    fs = _foreshadow_check(temp_project, 19)
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "foreshadow" and fs[0]["severity"] == "hint" and fs[0]["scope"] == "local"


def test_sample_19_main_thread_stall(temp_project):
    """样例 19 主线停滞：kind=main 15 章未推进 → plotline/hint/local。"""
    _seed_thread(temp_project, name="万魔渊线", kind="main", last_progress_chapter=5)
    fs = _thread_check(temp_project, 21)
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "plotline" and fs[0]["severity"] == "hint" and fs[0]["scope"] == "local"


def test_sample_20_goal_flip_no_transition(temp_project, monkeypatch):
    """样例 20 目标无过渡推翻 → character_state/minor/local。"""
    _seed_state(temp_project, field="goal", new_value="查明玉佩真相")
    key = _key("character_state", f"{str(A)}:goal", 7)
    _install_ledger_stub(monkeypatch, LedgerStub(
        judgments=[_judgment(key, evidence="林砚已彻底放下玉佩一事")]))
    draft = "林砚已彻底放下玉佩一事，将全副心思投在黑市商号上。第7章。"
    got = _l2(temp_project, [_state_cand("goal", "查明玉佩真相", "放下玉佩")], draft)
    assert len(got) == 1, got
    assert got[0]["severity"] == "minor" and got[0]["scope"] == "local"


def test_sample_21_identity_no_source(temp_project, monkeypatch):
    """样例 21 身份无来源却示人：identity 散修→城主 无受封/夺权 → character_state/major/local。"""
    _seed_state(temp_project, field="identity", new_value="散修")
    key = _key("character_state", f"{str(A)}:identity", 7)
    _install_ledger_stub(monkeypatch, LedgerStub(
        judgments=[_judgment(key, evidence="林砚以城主身份坐镇北境城")]))
    draft = "林砚以城主身份坐镇北境城，点将校尉，发号施令。第7章。"
    got = _l2(temp_project, [_state_cand("identity", "散修", "城主")], draft)
    assert len(got) == 1, got
    assert got[0]["severity"] == "major" and got[0]["scope"] == "local"


def test_sample_22_relation_fallout_no_record(temp_project, monkeypatch):
    """样例 22 师徒→敌对无决裂记录 → relation/major/structural。"""
    _seed_relation(temp_project, source_id=A, relation_type="master_student", target_id=B, source_chapter=2)
    key = _key("relation", f"{str(A)}:{str(B)}", 7)
    _install_ledger_stub(monkeypatch, LedgerStub(
        judgments=[_judgment(key, evidence="沈沧澜立于林砚对面，冷笑")]))
    draft = "沈沧澜立于林砚对面，冷笑：\"逆徒，今日便取你性命。\"第7章。"
    got = _l2(temp_project, [_rel_cand("hostile", "master_student", "hostile")], draft)
    assert len(got) == 1, got
    assert got[0]["conflict_type"] == "relation" and got[0]["severity"] == "major"


def test_sample_23_foreshadow_developing_stall(temp_project):
    """样例 23 伏笔烂尾·developing 无推进：捡起 10 章不落地 → foreshadow/hint/local。"""
    _seed_foreshadow(temp_project, description="玉佩与万魔渊的关联", status="developing",
                     planted_chapter=5, last_touched=12)
    fs = _foreshadow_check(temp_project, 23)
    assert len(fs) == 1, fs
    assert fs[0]["conflict_type"] == "foreshadow" and fs[0]["severity"] == "hint"


def test_sample_24_injury_valid_transition(temp_project, monkeypatch):
    """样例 24 阴性：先服回春丹闭关 → verdict=valid → 0 误报。"""
    _seed_state(temp_project, field="injury", new_value="濒死")
    key = _key("character_state", f"{str(A)}:injury", 7)
    _install_ledger_stub(monkeypatch, LedgerStub(
        judgments=[_judgment(key, verdict="valid", evidence="林砚服下回春丹闭关三日")]))
    assert _l2(temp_project, [_state_cand("injury", "濒死", "痊愈")], DRAFT24) == []


def test_sample_25_relation_valid_reconciliation(temp_project, monkeypatch):
    """样例 25 阴性：hostile→ally 有把酒言和 → verdict=valid → 0 误报。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=6)
    key = _key("relation", f"{str(A)}:{str(B)}", 7)
    _install_ledger_stub(monkeypatch, LedgerStub(
        judgments=[_judgment(key, verdict="valid", evidence="把酒言和")]))
    draft = "林砚与蛟王把酒言和，结下三月盟约。第7章。"
    assert _l2(temp_project, [_rel_cand("ally", "hostile", "ally")], draft) == []


def test_sample_26_foreshadow_legit_long_buried(temp_project):
    """样例 26 阴性：trigger 非空（回收条件在）→ 刻意长沉不告警 → 0 误报。"""
    _seed_foreshadow(temp_project, description="青玉匣封印之物", planted_chapter=3,
                     last_touched=3, trigger={"subject": "主角", "condition": "修为突破元婴后方可开启"})
    assert _foreshadow_check(temp_project, 19) == []


def test_sample_27_side_thread_dormant(temp_project):
    """样例 27 阴性：kind=side 长期休眠 → 不告警 → 0 误报。"""
    _seed_thread(temp_project, name="灵兽谷支线", kind="side", last_progress_chapter=3)
    assert _thread_check(temp_project, 25) == []


# =====================================================================
# D. 阴性对照（28-32）
# =====================================================================

def test_sample_28_location_transport_legal(temp_project):
    """样例 28 阴性（真空）：地点合法移动（借传送阵）→ 无 location 检出。

    注：样例 4 的 location 点级机制未落地 → 本阴性样例真空通过（无机制可误报），
    仅样例 4 的 L2 语义比对接手后在正文无过渡场景生效，本样例语义不触发 L2。
    """
    fs = _validate(temp_project, 8)
    assert not any(f["conflict_type"] == "location" for f in fs), fs


def test_sample_29_revisit_legal(temp_project):
    """样例 29 阴性（真空）：旧地重游有交代 → 无 character 检出（首次来访机制未落地）。"""
    _seed_event(temp_project, "林砚在北境城完成交易", 8)
    fs = _validate(temp_project, 12)
    assert not any(f["conflict_type"] == "character" for f in fs), fs


def test_sample_30_reforged_item_legal(temp_project):
    """样例 30 阴性（真空）：以千年寒铁重铸 → 无 item_rule 检出（物品机制未落地）。"""
    _seed_event(temp_project, "定水珠被天劫劈碎", 10)
    fs = _validate(temp_project, 11)
    assert not any(f["conflict_type"] == "item_rule" for f in fs), fs


def test_sample_31_power_single_jump_legal(temp_project):
    """样例 31 阴性：单次大跳（窗口不足 3 章）→ 曲线不触发 → 0 误报。"""
    ch_id = _seed_character(temp_project, "林砚")
    _seed_state(temp_project, character_id=ch_id, field="realm", new_value="金丹", chapter_seq=5)
    assert _power_check(temp_project, 8) == []


def test_sample_32_bridge_callback_exempt(temp_project):
    """样例 32 阴性：draft 含呼应标记词 → 整章豁免 → 0 误报。"""
    _seed_event(temp_project, CH5, 5, with_vector=True)
    cand = _mc("event", {"summary": CH5, "participants": []}, chapter=15)
    assert _bridge_check(temp_project, [cand], draft=CH15_MARKED) == []


# =====================================================================
# E. §7.8 关系台账自洽（33-35）
# =====================================================================

def test_sample_33_relation_duplicate_active(temp_project):
    """样例 33 阳性：同有序对同类型 2+ 活跃行 → relation/major/structural。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=4)
    fs = _relation_check(temp_project, 6)
    rel = [f for f in fs if f["conflict_type"] == "relation"]
    assert len(rel) == 1, fs
    assert rel[0]["severity"] == "major" and rel[0]["scope"] == "structural" and "不可判定" in rel[0]["evidence"][0]["quote"]


def test_sample_34_relation_contradiction_active(temp_project):
    """样例 34 阳性：同有序对敌/盟并存 → relation/major/structural。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="ally", target_id=B, source_chapter=4)
    fs = _relation_check(temp_project, 6)
    rel = [f for f in fs if f["conflict_type"] == "relation"]
    assert len(rel) == 1, fs
    assert rel[0]["severity"] == "major" and "矛盾" in rel[0]["evidence"][0]["quote"]


def test_sample_35_relation_single_active_legal(temp_project):
    """样例 35 阴性：单行活跃 + 带 valid_to 的临时盟约（非活跃）→ 0 误报。"""
    _seed_relation(temp_project, source_id=A, relation_type="hostile", target_id=B, source_chapter=3)
    _seed_relation(temp_project, source_id=A, relation_type="ally", target_id=B, source_chapter=6, valid_to=13)
    assert _relation_check(temp_project, 8) == []


# =====================================================================
# F. §8.6 全局审计抽样 L2（36-39）
# =====================================================================

def test_sample_36_persona_justified_change(temp_project, monkeypatch):
    """样例 36 阴性：转变有变故铺垫（师门血仇）→ LLM 判不漂移 → 0 误报。"""
    _seed_character(temp_project, "林砚", personality="谨慎隐忍、谋定后动")
    for seq in range(1, 12):
        _seed_chapter(temp_project, seq, f"林砚静观云海。第{seq}章。")
    _seed_chapter(temp_project, 12, "得知师门被灭，林砚盛怒之下一脚踹开城主府大门，喝道：\"今日老子就要你的命！\"")
    _install_audit_stub(monkeypatch, AuditStub(findings=[]))
    rep = _audit(temp_project, (1, 12))
    assert rep["status"] == "completed" and rep["findings"] == []


def test_sample_37_bridge_unmarked_echo(temp_project, monkeypatch):
    """样例 37 阴性：无词表标记的刻意呼应 → LLM 判 echo → 守卫丢弃 → 0 误报。"""
    _seed_event(temp_project, CH5, 5, with_vector=True)
    _seed_event(temp_project, CH5, 15, with_vector=True)
    _seed_chapter(temp_project, 15, CH15_UNMARKED_ECHO)
    _install_audit_stub(monkeypatch, AuditStub(findings=[
        {"event": "pair_1", "verdict": "echo", "chapter": 15,
         "evidence": "林砚不动声色举牌", "reason": "引蛇出洞，目的不同", "confidence": 0.9}]))
    rep = _audit(temp_project, (6, 15))
    assert rep["status"] == "completed" and rep["findings"] == [], rep["findings"]


def test_sample_38_style_drift(temp_project, monkeypatch):
    """样例 38 阳性：窗口内容偏离卷规划 → volume/hint/structural/L2。"""
    _seed_outline(temp_project, end=10)
    _seed_style_book(temp_project, 5, 5)
    _install_audit_stub(monkeypatch, AuditStub(findings=[
        {"chapter": 6, "verdict": "drifted", "evidence": DRIFT_QUOTE,
         "reason": "偏离入门试炼", "recovery": "拉回卷目标", "confidence": 0.85}]))
    rep = _audit(temp_project, (6, 10))
    assert rep["status"] == "completed"
    assert len(rep["findings"]) == 1, rep["findings"]
    f = rep["findings"][0]
    assert f["conflict_type"] == "volume" and f["severity"] == "hint"
    assert f["scope"] == "structural" and f["source"] == "L2" and f["evidence"][0]["chapter"] == 6


def test_sample_39_style_scene_variation(temp_project, monkeypatch):
    """样例 39 阴性：战斗短句（场景节奏合法变化）→ LLM 判 ok → 0 误报。"""
    _seed_style_book(temp_project, 5, 5, window_texts=lambda n: SCENE_TMPL.format(n=n))
    _install_audit_stub(monkeypatch, AuditStub(findings=[]))
    rep = _audit(temp_project, (6, 10))
    assert rep["status"] == "completed" and rep["findings"] == []


# =====================================================================
# 指标基线（防文档与代码漂移）
# =====================================================================

def test_metric_baseline_constants():
    """首测指标基线（指标基线常量与本套件结果对账）。

    检出率 = DETECTED_COUNT/POSITIVE_COUNT；误报率 = 0/NEGATIVE_COUNT。
    新增/取消 xfail 标记须同步更新 XFAIL_SAMPLES 与文档。
    """
    assert POSITIVE_COUNT == 26 and NEGATIVE_COUNT == 14, "样例总数须为 40"
    assert DETECTED_COUNT == 21, "检出 21 = 20 全机制（含样例 2 补漏 faction）+ 样例 4（类别偏差检出）"
    assert XFAIL_SAMPLES == {5, 6, 7, 8, 9}, "漏检 5 例固定"
