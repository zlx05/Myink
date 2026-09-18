"""单章子图节点实现（spec/state-flow.md 节点契约）。

确定性节点：load_state / recall / validate / persist —— 无 LLM，纯代码；
LLM 节点：plan_chapter / write / extract / revise / audit —— 走 ModelProvider 降级链。
LLM agent 不持**写**工具（§6.2 数据流边界）：Audit/Writer 持只读查证工具（§10，
function calling），写库只在 persist（编排层）发生。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

from pydantic import BaseModel, ValidationError
from langgraph.types import interrupt
from sqlalchemy.orm import Session

from aiink.config import settings
from aiink.context_budget import ContextBudgetExceeded, estimate_tokens
from aiink.db import tenant_session
from aiink.memory import repository as repo
from aiink.memory.correction import apply_memory_removal
from aiink.memory.embedder import get_embedder
from aiink.memory.invalidation import invalidate_chapter_memory
from aiink.memory.recall import build_context
from aiink.memory.vector_store import PgvectorStore
from aiink.models import (AgentRun, Alias, Chapter, Character, CharacterState, Entity,
                          Event, Fact, Foreshadow, Location, MemoryCandidate, Relation,
                          WritingLesson)
from aiink.models.memory import CHARACTER_STATE_FIELDS, RELATION_TYPES
from aiink.providers import FallbackChain, ModelResponse, make_chain
from aiink.providers.deepseek import strip_thinking_text
from aiink.schemas import AuditVerdict, ChapterPlan, Finding, MutationCandidate, ValidationReport
from aiink.validation.ledger_l2 import run_ledger_l2
from aiink.validation.continuity import check_transition_anchor, repair_generated_transition_anchor
from aiink.validation.service import ValidationService
from aiink.workflow import prompts
from aiink.workflow.outline import normalize_outline
from aiink.workflow.state import ChapterState
from aiink.workflow.streaming import ArtifactEmitter
from aiink.workflow.tools import READ_TOOLS, execute_tool

logger = logging.getLogger(__name__)

_CUMULATIVE_STATE_FIELDS = {"item", "knowledge"}
_STATE_REMOVAL_MARKERS = ("失去", "丢失", "交出", "消耗", "用掉", "不再持有", "移除", "删除")


def _materialize_cumulative_state(field: str, old_value: object, new_value: object) -> str:
    """把持有物/已知信息的“新增……其余不变”增量转成可直接落库的完整状态。"""
    old = str(old_value or "").strip()
    new = str(new_value or "").strip()
    if field not in _CUMULATIVE_STATE_FIELDS or not old or not new or old in new:
        return new
    if any(marker in new for marker in _STATE_REMOVAL_MARKERS):
        return new
    additive = any(marker in new for marker in ("新增", "增加", "获得", "得到", "另有"))
    unchanged_tail = "其余" in new and "不变" in new
    if not additive and not unchanged_tail:
        return new
    delta = re.sub(r"[；;，,]?\s*其余(?:物件|物品|信息|内容)?不变[。.]?", "", new).strip("；;，,。 ")
    return f"{old}；{delta}" if delta else old


# ---- 运行记录（§6.8：每节点一行 agent_runs 全字段 + 成本估算）----

def record_run(db: Session, *, project_id: str, task_id: str | None, node: str, role: str | None,
               resp: ModelResponse, error: str | None = None, detail: dict | None = None) -> None:
    db.add(AgentRun(
        project_id=uuid.UUID(project_id),
        task_id=task_id,  # thread_id（字符串，单章=task_id / 批次= batch:ch{seq}）
        node=node, role=role, model_id=resp.model_id,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
        cache_hit=resp.cache_hit, duration_ms=resp.duration_ms,
        cost_est=resp.cost_est, retry_count=resp.retry_count,
        degraded=resp.degraded, error=error, detail=detail,
    ))


def record_plain(db: Session, *, project_id: str, task_id: str | None, node: str,
                 detail: dict | None = None, duration_ms: int = 0) -> None:
    """确定性节点运行记录（无 LLM 调用：load_state/recall/validate/persist，§6.8 debug 全链路）。

    cost/token 为 0（不是 LLM 调用），detail 带执行统计（角色数/召回数/校验命中/落库数），
    让前端流转图能画出完整真实链路，而非只画 LLM 环节。
    """
    db.add(AgentRun(
        project_id=uuid.UUID(project_id),
        task_id=task_id, node=node,
        input_tokens=0, output_tokens=0, duration_ms=duration_ms,
        cost_est=0.0, detail=detail,
    ))


def record_run_detail(db: Session, *, task_id: str | None, node: str, detail: dict) -> None:
    """补记节点关键产物到最后一次该节点 agent_runs（如 audit verdict——解析后才产生）。

    合并而非覆盖：工具轮已写入 tool_trace，这里再叠加 verdict，保持 debug 信息完整。
    """
    if not task_id:
        return
    db.flush()  # 保证上一次 record_run 的 insert 对后续 query 可见（session autoflush=False）
    row = (db.query(AgentRun)
           .filter(AgentRun.task_id == task_id, AgentRun.node == node)
           .order_by(AgentRun.id.desc()).first())
    if row:
        row.detail = {**(row.detail or {}), **detail}


# 各节点 max_tokens 上限（§19.3：JSON mode 须设 max_tokens 防截断）。
# write 按 target_words 换算限长：实测中文约 1 token ≈ 0.7 字（1 字≈1.43 token），
# 3000 字 ≈ 2100 tokens；×1.25 余量防截断，同时从源头限死字数（最多 ~3900 字）。
_WRITE_TOKENS_PER_CHAR = 1.43
_MAX_TOKENS = {
    "plan_chapter": 8192, "extract": 4096, "revise": 8192, "audit": 8192,
    "reflexion": 4096, "summarize": 1024, "book_setup": 8192, "book_outline": 16384,
}


def _assistant_tool_calls_message(resp: ModelResponse) -> dict:
    """重建 assistant tool_calls 消息（§10 工具循环回传格式）。

    DeepSeek/OpenAI 要求下轮请求原样回传 assistant 的 tool_calls：arguments 须为
    JSON 字符串（Provider 已解析成 dict，这里重新 dumps）；thinking 已在 Provider
    关掉，无 reasoning_content 需回传（§19.3）。
    """
    return {
        "role": "assistant",
        "content": resp.content or "",
        "tool_calls": [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in (resp.tool_calls or [])
        ],
    }


def _bounded_generate(chain: FallbackChain, messages: list[dict], *,
                      streamer: ArtifactEmitter | None = None, **kwargs) -> ModelResponse:
    estimated = estimate_tokens({"messages": messages, "tools": kwargs.get("tools") or []})
    from aiink.providers.base import MODEL_REGISTRY
    limits = [MODEL_REGISTRY[m].context_window - (kwargs.get("max_tokens") or 8192) - 1000
              for m in chain.chain if m in MODEL_REGISTRY]
    budget = min([settings.request_token_budget, *limits])
    if estimated > budget:
        return ModelResponse(content="", model_id=chain.chain[0], error=(
            f"CONTEXT_BUDGET_EXCEEDED: 完整请求估算 {estimated} tokens，"
            f"预算 {budget}；未发送模型请求，请精简输入或调高 REQUEST_TOKEN_BUDGET。"))
    if streamer is not None:
        return chain.generate_stream(
            messages, on_delta=streamer.feed, on_reset=streamer.reset, **kwargs,
        )
    return chain.generate(messages, **kwargs)


def _run_tool_loop(db: Session, state: ChapterState, node: str, role: str, chain: FallbackChain,
                   messages: list[dict], *, max_tokens: int, tools: list[dict],
                   max_tool_calls: int, detail: dict | None = None,
                   final_json: bool = True,
                   streamer: ArtifactEmitter | None = None) -> tuple[ModelResponse, list[dict]]:
    """只读查证工具循环（§10）。

    单循环 + 条件最终轮：工具轮无 tool_calls 就直接用其 content（单轮，与无工具
    完全一致）；只有实际调了工具才追加「无 tools」最终轮输出最终结果。预算以工具执行
    总数计，超预算先把已发起的工具执行完再进最终轮（保证消息历史合法：不存在无 tool
    结果的 assistant tool_calls）。最终轮按节点区分输出格式：final_json=True（audit）
    强制严格 JSON；final_json=False（write）要求纯文本正文（=== CONTENT === 标记）。
    """
    messages = list(messages)
    used_tool = False
    tool_count = 0
    tool_trace: list[dict] = []
    while True:
        resp = _bounded_generate(
            chain, messages, json_mode=False, max_tokens=max_tokens, tools=tools,
            streamer=streamer,
        )
        # detail 带「到本轮为止已执行的工具」：每轮可审计调了哪些工具（§6.8 debug）
        record_run(db, project_id=state["project_id"], task_id=state.get("task_id"),
                   node=node, role=role, resp=resp, error=resp.error,
                   detail={**(detail or {}), "tool_trace": list(tool_trace)})
        if resp.error or not resp.tool_calls:
            return resp, tool_trace
        if streamer is not None:
            # 带 tool_calls 的 assistant content 只是查证过程说明，不属于最终 Plan/正文。
            # 下一轮生成前清空前端产物，避免把“我先查询……”混进用户看到的正文。
            streamer.reset()
        used_tool = True
        messages.append(_assistant_tool_calls_message(resp))
        for tc in resp.tool_calls:
            result = execute_tool(db, uuid.UUID(state["project_id"]), tc["name"], tc.get("arguments") or {})
            tool_trace.append({"tool": tc["name"], "arguments": tc.get("arguments") or {},
                               "result": result[:200]})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            tool_count += 1
        if tool_count >= max_tool_calls:
            break
    # 条件最终轮：去掉 tools（§10：最终轮带 tools 会诱导再次调工具）。按节点分输出格式：
    # audit 强制严格 JSON；write 纯文本正文（json_mode=False + 关思考，防思考抢占预算）。
    if streamer is not None:
        # 工具轮里可能包含模型的解释性 content，最终正文必须从干净缓冲开始。
        streamer.reset()
    if final_json:
        messages.append({"role": "system",
                         "content": "请基于工具核实结果直接输出最终严格 JSON，不要再调用工具。"})
        resp = _bounded_generate(
            chain, messages, json_mode=True, max_tokens=max_tokens,
            streamer=streamer,
        )
    else:
        messages.append({"role": "system",
                         "content": "请基于工具核实结果直接输出本章正文（纯文本散文，先输出独立一行 === CONTENT ===，禁止 JSON），不要再调用工具。"})
        resp = _bounded_generate(
            chain, messages, json_mode=False, max_tokens=max_tokens,
            disable_thinking=True, streamer=streamer,
        )
    record_run(db, project_id=state["project_id"], task_id=state.get("task_id"),
               node=node, role=role, resp=resp, error=resp.error,
               detail={**(detail or {}), "tool_trace": list(tool_trace)})
    return resp, tool_trace


def _llm(db: Session, state: ChapterState, node: str, role: str, chain: FallbackChain,
         messages: list[dict], *, tools: list[dict] | None = None,
         detail: dict | None = None,
         json_mode: bool = True, disable_thinking: bool = False,
         streamer: ArtifactEmitter | None = None) -> tuple[ModelResponse, list[dict]]:
    """统一 LLM 调用入口：带 tools 走只读查证工具循环，否则单次调用（§10）。

    json_mode 默认 True（plan/extract/audit 结构化输出）；write/revise 传 False
    走纯文本正文（=== CONTENT === 标记）。disable_thinking 仅纯文本路径需要：
    DeepSeek v4 思考模式默认开启，不关会抢占正文 token 预算（§19.3）。
    """
    max_tokens = _MAX_TOKENS.get(node)
    if node == "write":
        # 源头限长：按目标字数换算 token 上限（§6.9 防超写，§19.3 防截断）
        target = state.get("target_words") or 3000
        max_tokens = int(target * _WRITE_TOKENS_PER_CHAR * 1.25)
    if tools:
        return _run_tool_loop(db, state, node, role, chain, messages,
                              max_tokens=max_tokens, tools=tools,
                              max_tool_calls=settings.max_tool_calls, detail=detail,
                              final_json=json_mode, streamer=streamer)
    resp = _bounded_generate(chain, messages, json_mode=json_mode, max_tokens=max_tokens,
                          disable_thinking=disable_thinking, streamer=streamer)
    record_run(db, project_id=state["project_id"], task_id=state.get("task_id"),
               node=node, role=role, resp=resp, error=resp.error, detail=detail)
    return resp, []


# 节点级自修复重试上限（§6.12 调用层兜底第 4 档）：输出质量失败（非法 JSON / 空正文 / 跑偏）时重发次数。
_LLM_CHECK_RETRIES = 2
_RETRY_CORRECTIVE_JSON = ("你上一次输出未通过 JSON 语法、字段结构或内容约束校验。请保留其中已合格内容，"
                          "只修正下方具体校验错误后重新输出完整 JSON 对象；数组项和对象字段之间"
                          "必须使用英文逗号。不要包含 markdown 代码块围栏、解释文字或任何多余内容。")
_RETRY_CORRECTIVE_TEXT = ("你上一次输出为空、或不是纯文本正文（如 JSON / 工具调用文本），"
                          "请直接重新输出本章正文：纯文本散文，先输出独立一行 === CONTENT ===，禁止 JSON。")


def _llm_checked(db: Session, state: ChapterState, node: str, role: str, chain: FallbackChain,
                 messages: list[dict], *, check, tools: list[dict] | None = None,
                 json_mode: bool = True, disable_thinking: bool = False,
                 max_attempts: int = _LLM_CHECK_RETRIES,
                 corrective: str | None = None) -> tuple[ModelResponse, list[dict], Any, str | None]:
    """LLM 调用 + 输出自修复：check(content) 解析并返回产物，失败追加修正指令重试。

    调用层已有三档兜底（传输退避重试 / 模型降级链 flash→pro / _parse_json 容错），本函数补
    最后一档——「输出质量」失败（非法 JSON / 空正文 / 跑偏）时重发同请求 + 一行修正指令，
    靠模型方差与明确纠正挽回，避免一次坏输出永久杀掉整章（用户实测：plan_chapter 解析失败、
    DeepSeek 空内容 → 任务直接失败）。

    契约：check(content) -> 解析产物（失败抛异常）。**只重试解析层失败，resp.error 直接透传**
    （降级链已用尽，重试同一模型无意义；且 BatchFailStub 建模「失败一次→批次失败」语义，
    误重试会吃掉失败）。每次重试都走 _llm（record_run 记一条，前端流转图可见重试），
    最终失败 error 带 retry 次数 + 末次 content 片段供排查。
    """
    last_exc: str | None = None
    last_content: str | None = None
    for attempt in range(max_attempts + 1):
        msgs = list(messages)
        if attempt:
            # JSON 自修复必须让模型看到上一版输出；旧逻辑只给错误文本、不给原文，模型只能
            # 从头重写整份 Plan，容易连续生成同一种漏逗号错误，也让前端看起来反复规划。
            if json_mode and last_content:
                msgs.append({"role": "assistant", "content": last_content})
            msgs.append({"role": "system",
                         "content": (corrective or (_RETRY_CORRECTIVE_JSON if json_mode
                                                    else _RETRY_CORRECTIVE_TEXT)) + f"\n具体校验错误：{last_exc}"})
        streamer = None
        if node in ("plan_chapter", "write", "revise") and state.get("chapter_seq") is not None:
            # Plan 的 attempt 表示业务上的规划版本（初版=1，replan=2...），让前端
            # 在新版流式到达时把旧版折叠。解析自修复仍沿用同一个业务版本和新 artifact_id。
            artifact_attempt = state.get("replan_count", 0) + 1 \
                if node == "plan_chapter" else attempt + 1
            streamer = ArtifactEmitter(
                task_id=state.get("task_id") or "",
                chapter_seq=state["chapter_seq"],
                stage="plan" if node == "plan_chapter" else "write",
                attempt=artifact_attempt,
            )
            streamer.start()
        resp, tool_trace = _llm(db, state, node, role, chain, msgs,
                                tools=tools, json_mode=json_mode,
                                disable_thinking=disable_thinking, streamer=streamer)
        if resp.error:
            if streamer is not None:
                streamer.failed(resp.error)
            return resp, tool_trace, None, resp.error
        last_content = resp.content
        try:
            value = check(resp.content)
            if streamer is not None:
                streamer.complete(value if node == "plan_chapter" else None)
            return resp, tool_trace, value, None
        except Exception as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
            if streamer is not None:
                streamer.failed(last_exc)
            logger.warning("%s 输出不合格（attempt=%d/%d）: %s", node, attempt, max_attempts, last_exc)
    return resp, [], None, (f"{node} 输出多次不合格（已重试 {max_attempts} 次）: {last_exc}"
                            f" | content[:120]={resp.content[:120]!r}")


def _split_marked(content: str) -> dict[str, str]:
    """按 === 标记切块（write/revise 纯文本正文，§6.12 标记锚点）。

    独立行 ``=== 标记名 ===`` 起一个新块，块内文本归该标记；标记行前的杂质文本
    归 "default"。无任何标记 → 整段归 "default"（模型漏写标记时兜底不丢章）。
    块文本保留原始换行，仅去掉首尾空行。
    """
    parts: dict[str, str] = {}
    cur_key: str | None = None
    cur_buf: list[str] = []
    for line in content.split("\n"):
        # 标记名大小写不敏感（模型偶发输出小写/中文标记行，兜底归 default 会污染正文）
        m = re.match(r"^=== ([a-zA-Z_一-鿿]+) ===\s*$", line.strip())
        if m:
            if cur_key is not None:
                parts[cur_key] = "\n".join(cur_buf).strip("\n")
            cur_key = m.group(1).upper()
            cur_buf = []
        else:
            if cur_key is None:
                cur_key = "default"
            cur_buf.append(line)
    if cur_key is not None:
        parts[cur_key] = "\n".join(cur_buf).strip("\n")
    return parts


def _content_block(parts: dict[str, str], raw: str) -> str:
    """取正文块（write/revise）：优先 CONTENT 标记，其次 default（模型漏写标记兜底），
    再次第一个非空命名块（模型写错标记名如 === 正文 === 时仍取到正文、标记行不落库），
    最后回退整段原文。"""
    for key in ("CONTENT", "default"):
        if key in parts and parts[key]:
            return parts[key]
    for v in parts.values():
        if v:
            return v
    return raw


# 模型把工具调用写成文本（未走 function calling 协议）的强特征（§10 单轮/最终轮跑偏）。
# 网文正文几乎不可能含这些 XML/函数调用痕迹，命中即判 write/revise 跑偏、显式失败，
# 防止把噪声当正文落库。
_TOOL_NOISE_MARKERS = ("<ai_output>", "<function_results>", "<invoke name=", "<tool_use",
                       "<tool_calls>", "<DSML", "|assistant|")


def _looks_like_tool_noise(text: str) -> bool:
    """DeepSeek 偶发在 tools 上下文里输出文本式工具调用（而非返回 tool_calls）。

    场景：`_run_tool_loop` 单轮直出（模型没调工具但写了"调用了 inspect_facts…"）或
    最终轮跑偏，把 `<ai_output>...<invoke name=...>` 混进正文 → 无标记归 default →
    垃圾当正文。命中强特征即显式失败（诚实报错可重发），不落库污染。
    """
    return any(m in text for m in _TOOL_NOISE_MARKERS)


def _parse_json(content: str) -> Any:
    """鲁棒 JSON 解析（§6.12 输出容错）。

    真实 LLM 偶发在 JSON 前/后附杂质（markdown 围栏、语气词）、尾部含 ``}`` 的废话、
    或字符串里放原始控制字符（DeepSeek 偶发未转义换行 → 非法 JSON）、
    正文里放裸 ASCII 双引号（字符串被提前闭合 → Expecting ',' delimiter）。
    raw_decode 只解析首个完整 JSON 值，天然截断尾部杂质；控制字符转义、裸引号转义依次兜底。
    """
    decoder = json.JSONDecoder()
    try:
        return decoder.raw_decode(content)[0]
    except json.JSONDecodeError as original_exc:
        start = content.find("{")
        if start == -1:
            raise
        raw_body = content[start:]
        # 依次尝试最小修复：原结构 → 控制字符 → 裸引号+控制字符。每一种候选都允许
        # 在明确的 JSON 值边界补漏逗号；不修改字符串内容，也不凭语义补字段。
        candidates = [raw_body, _escape_control_chars(raw_body)]
        candidates.append(_escape_control_chars(_repair_stray_quotes(raw_body)))
        last_exc = original_exc
        seen: set[str] = set()
        for body in candidates:
            if body in seen:
                continue
            seen.add(body)
            try:
                return _raw_decode_repair_missing_commas(decoder, body)
            except json.JSONDecodeError as exc:
                last_exc = exc
        raise last_exc


def _raw_decode_repair_missing_commas(decoder: json.JSONDecoder, body: str,
                                       max_repairs: int = 16) -> Any:
    """只修复解析器明确指出的 JSON 漏逗号，避免为一次语法瑕疵重跑整个 Planner。

    可修复边界示例：``["甲" "乙"]``、``{"a": 1 "b": 2}``、``[{} {}]``。
    仅当错误位置右侧能开始一个 JSON 值，且左侧刚结束一个 JSON 值时插入英文逗号；
    其他错误原样抛出，交给节点级模型自修复，防止把正文裸引号等问题误改成结构。
    """
    repaired = body
    last_exc: json.JSONDecodeError | None = None
    for _ in range(max_repairs + 1):
        try:
            return decoder.raw_decode(repaired)[0]
        except json.JSONDecodeError as exc:
            last_exc = exc
            if exc.msg != "Expecting ',' delimiter":
                raise
            right = exc.pos
            while right < len(repaired) and repaired[right].isspace():
                right += 1
            left = right - 1
            while left >= 0 and repaired[left].isspace():
                left -= 1
            if left < 0 or right >= len(repaired):
                raise
            previous = repaired[left]
            following = repaired[right]
            value_ended = previous in '\"}]' or previous.isdigit() \
                or repaired[:left + 1].endswith(("true", "false", "null"))
            value_starts = following in '\"{[' or following.isdigit() \
                or following in "-tfn"
            if not (value_ended and value_starts):
                raise
            repaired = repaired[:right] + "," + repaired[right:]
    assert last_exc is not None
    raise last_exc


def _escape_control_chars(s: str) -> str:
    """JSON 字符串值内原始控制字符转义（§6.12 输出容错，§19.3 思考兜底必踩）。

    状态机：只在 JSON 字符串值内转义 0x00-0x1F / 0x7F 单字节控制字符；
    结构空白 ``\\t``/``\\n``/``\\r`` 原样保留（JSON 结构空白只允许这三个原始字符）。
    已转义的 ``\\n`` 是反斜杠+n 两字符，状态机按转义对跳过，不误伤。

    旧实现用 ``[\\x00-\\x1f\\x7f]`` 无差别正则替换，把结构位置的合法换行也转成
    ``\\u000a`` 字面量 —— 字面量在结构位置非法（结构空白不接受转义序列），
    导致「前导杂质 + 多行 JSON」在容错路径必挂：audit 的 reasoning_content 兜底
    是带前导思考文本的多行 JSON，2026-08-10 演示逼出（Expecting property name
    line 1 column 2）。仅当字符串值内原始换行/制表符 → \\uXXXX 还原为字符串内容。
    """
    out: list[str] = []
    in_str = False
    escaped = False
    for ch in s:
        if escaped:  # 字符串值内转义对的后半（\" \\\\ \\uXXXX 的后续），原样跳过
            out.append(ch)
            escaped = False
            continue
        if in_str and ch == "\\":
            escaped = True
            out.append(ch)
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if not in_str and ch in ("\t", "\n", "\r"):
            out.append(ch)  # JSON 结构空白：合法，保留
            continue
        if in_str and ("\x00" <= ch <= "\x1f" or ch == "\x7f"):
            out.append("\\u%04x" % ord(ch))
            continue
        out.append(ch)
    return "".join(out)


def _repair_stray_quotes(s: str) -> str:
    """JSON 字符串值内裸引号转义兜底（§6.12 输出容错）。

    DeepSeek 偶发在正文里用 ASCII 双引号（应转义而未转义）→ 字符串被提前闭合，
    json 报 ``Expecting ',' delimiter``。状态机：在字符串值内，裸 ``"`` 后跟 ``, : } ]``
    之一视为结构闭合符（保留），否则视为正文引号（补反斜杠转义）。已转义的 ``\\"``/``\\\\``/``\\uXXXX``
    原样跳过不误伤。仅在常规容错后兜底，合法 JSON 走不到这里。
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if ch == "\\" and i + 1 < n:  # 转义对（\" \\\\ \\uXXXX）整对跳过
                out.append(ch)
                out.append(s[i + 1])
                i += 2
                continue
            if ch == '"':
                # 合法 JSON 允许闭引号与 `, : } ]` 之间有空白/换行；看下一个非空白符，
                # 否则会把格式化 JSON 的正常闭引号误当成正文裸引号。
                j = i + 1
                while j < n and s[j].isspace():
                    j += 1
                nxt = s[j] if j < n else ""
                if nxt in ',:}]':
                    in_str = False  # 后随结构分隔符 → 真正的字符串闭合引号
                    out.append(ch)
                else:
                    out.append("\\")  # 正文里的裸引号 → 转义保留
                    out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
        i += 1
    return "".join(out)


def _coerce_str_lists(data: dict, model: type[BaseModel]) -> dict:
    """§6.12 输出容错：把 schema 的 list[str] 字段归一化（LLM 偶发写成对象数组）。

    真实 DeepSeek 会把 ``hooks_to_plant`` 返回成 ``[{"hook": "..."}]`` 而非 ``["..."]``，
    直接过 pydantic 校验失败。这里只对 list[str] 字段取主要文本文值，其余字段不动。
    """
    str_list_fields = {n for n, f in model.model_fields.items() if f.annotation == list[str]}
    for field in str_list_fields:
        items = data.get(field)
        if not isinstance(items, list):
            continue
        coerced: list[str] = []
        for it in items:
            if isinstance(it, str):
                coerced.append(it)
            elif isinstance(it, dict):
                # 优先取常见文本键；没有则取「唯一字符串值」兜底（键名不可控，值内容才可信）
                text = next((it[k] for k in ("hook", "goal", "event", "constraint", "text", "content",
                                             "title", "description", "summary")
                             if isinstance(it.get(k), str)), None)
                if text is None:
                    str_values = [v for v in it.values() if isinstance(v, str)]
                    if len(str_values) == 1:
                        text = str_values[0]
                if text:
                    coerced.append(text)
            # 其他类型（数字/嵌套对象）丢弃，保持下游 contract 稳定
        data[field] = coerced
    return data


def _resolve_character_id(db: Session, project_id: str, name: str) -> uuid.UUID | None:
    """人名 → canonical id（§7.5 归一化；找不到则不落该候选，不猜）。"""
    ch = repo.get_character(db, uuid.UUID(project_id), name)
    return ch.id if ch else None


# ---- 确定性节点 ----

def node_load_state(state: ChapterState) -> ChapterState:
    pid = state["project_id"]
    with tenant_session(pid) as db:
        characters = [
            {"id": str(c.id), "name": c.name, "realm_cap": c.realm_cap, "personality": c.personality}
            for c in repo.get_all_characters(db, uuid.UUID(pid))
        ]
        settings_row = repo.get_settings(db, uuid.UUID(pid))
        world_rules = (settings_row.world_rules if settings_row else {}) or {}
        style_profile = (settings_row.style_profile if settings_row else {}) or {}
        genre_pack = (settings_row.genre_pack if settings_row else {}) or {}
        project = repo.get_project(db, uuid.UUID(pid))
        target_words = project.target_words if project and project.target_words else None
        plan_input = {"characters": characters, "world_rules": world_rules}
        record_plain(db, project_id=pid, task_id=state.get("task_id"), node="load_state",
                     detail={"characters": len(characters)})
        return {"characters": characters, "chapter_plan_input": plan_input, "settings": world_rules,
                "style_profile": style_profile, "genre_pack": genre_pack, "target_words": target_words,
                # 重置上次尝试的瞬态失败标记（§6.12 续跑）：同 thread 再 invoke 时
                # LangGraph 合并 checkpoint 状态，残留 error 会让 write/extract 短路重蹈失败 → 这里清零
                "error": None, "needs_review": False, "persisted": False, "unresolved": [],
                "revision_count": 0, "replan_count": 0, "audit_verdict": None,
                "replan_batch": False, "report": None, "candidates": [], "draft": None}


def _merge_unresolved(prev: list[dict], audit_findings: list[Finding]) -> list[dict]:
    """合并 unresolved（§6.5）：保留既有 L1/L2 major，audit 判定覆盖同 conflict_key。

    修复点级 L2 进入 revise 上下文的前置缺口：node_audit 曾以 verdict.findings 整体覆盖
    state["unresolved"]，把 validate 的 L1/L2 major 丢出 node_revise 输入（node_revise:605
    只读 unresolved）。合并后不同键共存、同键以 audit（语义层）为准。
    """
    by_key: dict[str, dict] = {f["conflict_key"]: f for f in prev}
    for f in audit_findings:
        if f.severity in ("critical", "major"):
            by_key[f.conflict_key] = f.model_dump(mode="json")
    return list(by_key.values())


def node_recall(state: ChapterState) -> ChapterState:
    pid = state["project_id"]
    participants = [c.get("name") for c in state.get("characters", [])[:12]]
    shared = state.get("shared_context") or {}
    with tenant_session(pid) as db:
        ctx = build_context(
            db, project_id=uuid.UUID(pid), chapter_seq=state["chapter_seq"],
            participants=participants, user_instruction=state.get("user_instruction"),
            shared_context=shared,
        )
        out = {"context": ctx.model_dump(mode="json")}
        # 批次级共享池：首章组装后回写，供批次层桥给后续章复用（§6.11，稳定部分一次组装）
        if shared:
            out["shared_context"] = shared
        record_plain(db, project_id=pid, task_id=state.get("task_id"), node="recall",
                     detail={"facts": len(ctx.long_term_facts), "events": len(ctx.mid_term_events),
                             "snapshots": len(ctx.entity_snapshots),
                             "settings": len(ctx.setting_snapshots),
                             "foreshadows": len(ctx.open_foreshadows),
                             "threads": len(ctx.plot_threads), "context_tokens_est": ctx.token_usage})
        return out


def node_validate(state: ChapterState) -> ChapterState:
    pid = state["project_id"]
    realm_order = (state.get("settings") or {}).get("realm_order", [])
    with tenant_session(pid) as db:
        candidates = [MutationCandidate(**c) for c in state.get("candidates", [])]
        plan = ChapterPlan(**state["plan"]) if state.get("plan") else None
        if state.get("error"):
            return {"report": ValidationReport(
                project_id=uuid.UUID(pid), chapter_seq=state["chapter_seq"],
            ).model_dump(), "unresolved": []}
        service = ValidationService(realm_order=realm_order)
        report = service.validate(db, project_id=uuid.UUID(pid), chapter_seq=state["chapter_seq"],
                                  candidates=candidates, plan=plan,
                                  draft=state.get("draft"), target_words=state.get("target_words"))
        # 正文-台账语义比对 L2（§8.6 点级，样例 16/17/20/21/22）：判定集预滤零成本短路、
        # LLM 非阻断（L1 窄脚印仍在）。LLM 不能进 validate（validate 被测试直接调用）→ 在此接线。
        l2_findings = run_ledger_l2(db, project_id=uuid.UUID(pid), chapter_seq=state["chapter_seq"],
                                    candidates=state.get("candidates", []), draft=state.get("draft"),
                                    task_id=state.get("task_id"))
        if l2_findings:
            report.findings.extend(Finding(**f) for f in l2_findings)
            report.summary["total"] = report.summary.get("total", 0) + len(l2_findings)
            # l2_major 进 summary → route_after_audit 路由 revise / persist 转人工（§6.11）
            report.summary["l2_major"] = sum(1 for f in l2_findings if f.get("severity") == "major")
        from aiink.workflow.review import rejected_proposal
        for cand in state.get("candidates", []):
            rejected = rejected_proposal(db, pid, state["chapter_seq"], cand)
            if rejected and (rejected.review or {}).get("mode") == "revise":
                report.findings.append(Finding(
                    conflict_key=f"author:{rejected.id}", conflict_type="fact", severity="critical",
                    scope="local", source="L1", evidence=[],
                    suggestion=f"作者已否定该设定：{rejected.payload}；{(rejected.review or {}).get('reason', '')}。请修订正文并重新抽取。"))
                report.summary["critical"] = report.summary.get("critical", 0) + 1
                report.summary["total"] = report.summary.get("total", 0) + 1
        # unresolved = critical/major（触发 revise；hint 不阻塞）
        unresolved = [f.model_dump(mode="json") for f in report.findings if f.severity in ("critical", "major")]
        # 校验报告落库（findings 证据链可追溯，§8.7）
        record_plain(db, project_id=pid, task_id=state.get("task_id"), node="validate",
                     detail={"findings_total": report.summary.get("total", 0),
                             "critical": report.summary.get("critical", 0),
                             "l2_major": report.summary.get("l2_major", 0),
                             "unresolved": len(unresolved)})
        return {"report": report.model_dump(mode="json"), "unresolved": unresolved}


def node_audit(state: ChapterState) -> ChapterState:
    """审核中枢（§6.5/§6.11 混合路由的 LLM 语义层）：L2 语义校验 + 剧情/质量判断 + 路由决策。

    输出 AuditVerdict（pass/rewrite/replan）做语义路由；L1 critical 与轮次预算由
    route_after_audit 规则层强制（Audit 不能绕过硬约束）。决策 reasons/confidence 落库可审计。
    """
    if state.get("error"):
        return {}  # 上游 LLM 已失败：透传根因（§6.12）
    pid = state["project_id"]
    draft = state.get("draft")
    if not draft:
        return {"error": "write 未产出草稿"}

    def _check_audit(content: str) -> AuditVerdict:
        data = _parse_json(content)
        return AuditVerdict(**{k: v for k, v in data.items() if k in AuditVerdict.model_fields})

    with tenant_session(pid) as db:
        _, tool_trace, verdict, err = _llm_checked(
            db, state, "audit", "Audit", make_chain("audit", db=db, project_id=pid),
            prompts.audit_messages(draft, state.get("plan") or {},
                                   {**(state.get("context") or {}), "validation_report": state.get("report")},
                                   state["chapter_seq"], genre_pack=state.get("genre_pack")),
            check=_check_audit, tools=READ_TOOLS)
        if err:
            return {"error": err}
        # 审核决策补记到本条 agent_runs（§6.8 debug：verdict/reasons/confidence 落库可审计）
        record_run_detail(db, task_id=state.get("task_id"), node="audit",
                          detail={"audit_verdict": verdict.model_dump(mode="json")})
    # verdict.findings（L2）→ unresolved：rewrite 时 revise 注入逐条修（§6.5）。
    # 合并而非覆盖：保留 validate 的 L1/L2 major（此前被 audit 覆盖丢出 revise 上下文），
    # 同 conflict_key 以 audit 判定为准（语义层更权威）。
    unresolved = _merge_unresolved(state.get("unresolved", []), verdict.findings)
    out: dict = {
        "audit_verdict": verdict.model_dump(mode="json"),
        "unresolved": unresolved,
        "replan_batch": verdict.verdict == "replan" and verdict.replan_target == "batch",
    }
    # 只读查证工具调用痕迹（§10）：可审计 + 佐证 Agent 确实核实过
    if tool_trace:
        out["tool_trace"] = tool_trace
    return out


# ---- LLM 节点 ----

def _load_outline_slice(db: Session, project_id: uuid.UUID, chapter_seq: int) -> dict | None:
    """整书大纲按章切片：{objective, volume, stage}；无大纲 → None。"""
    from aiink.workflow.outline import covering_item

    row = repo.get_volume_outline(db, project_id, 1)
    if row is None or not isinstance(row.outline, dict):
        return None
    outline = normalize_outline(row.outline) or {}
    volumes = [v for v in (outline.get("volumes") or []) if isinstance(v, dict)]
    if not volumes:
        return None
    volume = covering_item(volumes, chapter_seq)
    if volume is None:
        return None
    stage = covering_item(list(volume.get("stages") or []), chapter_seq)
    return {"objective": outline.get("objective") or "", "volume": volume, "stage": stage}


def node_plan_chapter(state: ChapterState) -> ChapterState:
    pid = state["project_id"]
    seq = state["chapter_seq"]

    def _check_plan(content: str) -> dict:
        data = _coerce_str_lists(_parse_json(content), ChapterPlan)
        plan = ChapterPlan(**{k: v for k, v in data.items() if k in ChapterPlan.model_fields})
        plan.project_id = uuid.UUID(pid)
        plan.chapter_seq = seq
        data = plan.model_dump(mode="json")
        data = repair_generated_transition_anchor(data, state.get("context") or {})
        check_transition_anchor(data, state.get("context") or {})
        return data

    with tenant_session(pid) as db:
        # 整书大纲注入（§11）：规划须贴本章大纲位、沿所属卷目标/KR 推进；无大纲则回退原行为。
        # 扫榜灵感已整体前移至建书前（§10：题材风向参考只作建书向导工具，不再注入规划节点）
        outline = _load_outline_slice(db, uuid.UUID(pid), seq)
        _, _, plan, err = _llm_checked(
            db, state, "plan_chapter", "Planner", make_chain("planner", db=db, project_id=pid),
            prompts.plan_messages(state.get("context") or {}, state.get("batch_goal"),
                                  outline=outline),
            check=_check_plan)
        if err:
            return {"error": err}
        record_run_detail(
            db, task_id=state.get("task_id"), node="plan_chapter",
            detail={
                "plan": plan,
                "plan_attempt": state.get("replan_count", 0) + 1,
                "writing_mode": state.get("writing_mode", "auto"),
            },
        )
    return {"plan": plan}


def node_plan_gate(state: ChapterState) -> ChapterState:
    """手动模式计划确认点；自动模式零成本直通。

    interrupt 位于独立节点，恢复时只会重放本节点，不会再次调用 Planner。审核触发
    replan 后会再次经过这里，因此每一版新计划都需要重新确认。
    """
    if state.get("error") or state.get("writing_mode", "auto") != "manual":
        return {}
    original = state.get("plan") or {}
    approved = interrupt({
        "kind": "chapter_plan_review",
        "chapter_seq": state["chapter_seq"],
        "attempt": state.get("replan_count", 0) + 1,
        "plan": original,
    })
    pid = state["project_id"]
    plan = ChapterPlan.model_validate(approved)
    plan.project_id = uuid.UUID(pid)
    plan.chapter_seq = state["chapter_seq"]
    normalized = plan.model_dump(mode="json")
    check_transition_anchor(normalized, state.get("context") or {})
    with tenant_session(pid) as db:
        chapter = repo.get_chapter(db, uuid.UUID(pid), state["chapter_seq"])
        if chapter is not None and not (chapter.content or "").strip():
            chapter.status = "writing"
        record_plain(
            db, project_id=pid, task_id=state.get("task_id"), node="plan_review",
            detail={
                "status": "confirmed",
                "plan_attempt": state.get("replan_count", 0) + 1,
                "changed": normalized != original,
                "original_plan": original,
                "approved_plan": normalized,
            },
        )
    return {"plan": normalized}


def _check_draft(content: str) -> str:
    """write/revise 正文校验（§6.12 输出质量）：取 CONTENT 块，空/工具噪声抛异常触发 _llm_checked 重试。

    复用 _split_marked/_content_block 容错（模型漏写标记兜底归 default），跑偏命中强特征
    显式失败（诚实报错可重试），不把噪声当正文落库。
    """
    parts = _split_marked(strip_thinking_text(content))
    draft = strip_thinking_text(_content_block(parts, content))
    if not draft:
        raise ValueError("正文为空")
    if _looks_like_tool_noise(draft):
        raise ValueError("正文跑偏（工具调用文本而非正文）")
    return draft


def node_write(state: ChapterState) -> ChapterState:
    if state.get("error"):
        return {}  # 上游 LLM 已失败：透传根因，不再覆盖（§6.12 错误可追溯）
    pid = state["project_id"]
    with tenant_session(pid) as db:
        outline = _load_outline_slice(db, uuid.UUID(pid), state["chapter_seq"])
        _, tool_trace, draft, err = _llm_checked(
            db, state, "write", "Writer", make_chain("writer", db=db, project_id=pid),
            prompts.write_messages(
                state.get("context") or {}, state.get("plan") or {},
                style_profile=state.get("style_profile"), target_words=state.get("target_words"),
                outline=outline, genre_pack=state.get("genre_pack"),
            ),
            check=_check_draft, tools=READ_TOOLS, json_mode=False, disable_thinking=True,
            corrective=_RETRY_CORRECTIVE_TEXT)
        if err:
            return {"error": err}
    out: dict = {"draft": draft}
    if tool_trace:
        out["tool_trace"] = tool_trace
    return out


def extract_candidates_from_draft(db: Session, *, project_id: str, chapter_seq: int,
                                  draft: str, task_id: str | None = None,
                                  context: dict | None = None) -> tuple[list[dict], str | None]:
    """对任意正文跑记忆抽取（extract LLM）→ 校验/归一化候选（§6.4/§7.5）。

    节点与「校正记忆」端点复用同一条抽取/清洗路径，保证口径一致（阶段 3 编辑校正）。
    context（recall 的 RetrievedContext）非空时注入当前台账快照——extract 校准 old_value、
    产出 relation_change 候选（正文-台账语义比对 L2 的证据链入口，§8.6）。
    返回 (candidates, error)：error 非空时 candidates 为空，由调用方决定是否阻断。
    """
    try:
        messages = prompts.extract_messages(draft, chapter_seq, context=context)
    except ContextBudgetExceeded as exc:
        return [], str(exc)
    state: dict = {"project_id": project_id, "chapter_seq": chapter_seq, "task_id": task_id}

    def _check_extract(content: str) -> list[dict]:
        data = _parse_json(content)
        if not isinstance(data, dict):
            raise ValueError("extract 输出不是 JSON 对象")
        rows = data.get("candidates", []) or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("candidates 必须是对象数组")
        return rows

    _, _, raw_candidates, err = _llm_checked(
        db, state, "extract", "Memory", make_chain("extract", db=db, project_id=project_id),
        messages, check=_check_extract)
    if err:
        return [], err

    candidates: list[dict] = []
    for raw in raw_candidates:
        try:
            cand = MutationCandidate(**raw)
        except ValidationError:
            continue  # 坏候选拒绝但不崩（§6.12 数据层）
        if cand.kind == "character_state":
            cid = _resolve_character_id(db, project_id, str(cand.payload.get("character_id", "")))
            if not cid:
                continue  # 无法归一化的角色不落库
            # field 白名单校验（§6.12 坏候选拒绝但不崩）：LLM 自由输出可能造出
            # DB CHECK 枚举外的新值（如 "realm_status"），落库必炸批次 → 这里丢弃
            if cand.payload.get("field") not in CHARACTER_STATE_FIELDS:
                logger.warning("丢弃非法 character_state 候选 field=%r（ch%d）",
                               cand.payload.get("field"), chapter_seq)
                continue
            cand.payload["character_id"] = str(cid)
            field = str(cand.payload["field"])
            current = repo.get_character_state(
                db, uuid.UUID(project_id), cid, max(0, chapter_seq - 1)
            ).get(field)
            if current is not None:
                # old_value 以数据库台账为准，不能信任模型自行复述的残缺旧值。
                cand.payload["old_value"] = current
            cand.payload["new_value"] = _materialize_cumulative_state(
                field, cand.payload.get("old_value"), cand.payload.get("new_value")
            )
        elif cand.kind == "relation_change":
            # 双端角色归一化 + relation_type 白名单（§6.12）：任一缺 → 丢，防 persist 落库炸批次
            src = _resolve_character_id(db, project_id, str(cand.payload.get("source_id", "")))
            tgt = _resolve_character_id(db, project_id, str(cand.payload.get("target_id", "")))
            if not src or not tgt:
                logger.warning("丢弃无法归一化的 relation_change 候选（ch%d）", chapter_seq)
                continue
            if cand.payload.get("relation_type") not in RELATION_TYPES:
                logger.warning("丢弃非法 relation_change 候选 relation_type=%r（ch%d）",
                               cand.payload.get("relation_type"), chapter_seq)
                continue
            cand.payload["source_id"] = str(src)
            cand.payload["target_id"] = str(tgt)
        elif cand.kind == "character_card":
            # 新人物卡片去重（§7.11 ④）：名字已建档（Character 表）→ 不抽为候选。
            # 缺此守卫时 resume 重跑会把已确认角色反复抽成新卡 → persist 恒 has_card
            # → 章节永远 awaiting_review、正文永远不落库（死循环）。
            name = str((cand.payload or {}).get("name") or "").strip()
            if not name:
                continue  # 空名卡片是噪声（persist 也不暂停，审计 Low-4）
            if _resolve_character_id(db, project_id, name):
                logger.info("丢弃 character_card 候选（名字已建档 %r，ch%d）", name, chapter_seq)
                continue
        from aiink.workflow.review import rejected_proposal
        rejected = rejected_proposal(db, project_id, chapter_seq, cand.model_dump(mode="json"))
        if rejected and (rejected.review or {}).get("mode") != "revise":
            continue
        candidates.append(cand.model_dump(mode="json"))
    return candidates, None


def node_extract(state: ChapterState) -> ChapterState:
    if state.get("error"):
        return {}  # 上游 LLM 已失败：透传根因（§6.12）
    pid = state["project_id"]
    draft = state.get("draft")
    if not draft:
        return {"error": "write 未产出草稿"}
    with tenant_session(pid) as db:
        candidates, err = extract_candidates_from_draft(
            db, project_id=pid, chapter_seq=state["chapter_seq"], draft=draft,
            task_id=state.get("task_id"), context=state.get("context"))
        if err:
            return {"error": err}
    return {"candidates": candidates}


def node_revise(state: ChapterState) -> ChapterState:
    pid = state["project_id"]
    context = dict(state.get("context") or {})
    reasons = (state.get("audit_verdict") or {}).get("reasons") or []
    if reasons:
        context["short_context"] = [*(context.get("short_context") or []),
                                    {"kind": "audit_feedback", "text": "修订原因：" + "；".join(reasons)}]
    with tenant_session(pid) as db:
        resp, _, draft, err = _llm_checked(
            db, state, "revise", "Writer", make_chain("writer", db=db, project_id=pid),
            prompts.revise_messages(state["draft"], state.get("unresolved", []),
                                    state["chapter_seq"], context=context,
                                    plan=state.get("plan"), style_profile=state.get("style_profile"),
                                    target_words=state.get("target_words"),
                                    outline=_load_outline_slice(db, uuid.UUID(pid), state["chapter_seq"]),
                                    genre_pack=state.get("genre_pack")),
            check=_check_draft, json_mode=False, disable_thinking=True,
            corrective=_RETRY_CORRECTIVE_TEXT)
        if err:
            return {"error": err}
    parts = _split_marked(resp.content)
    responses = []
    raw_responses = parts.get("RESPONSES")
    if raw_responses:
        try:
            parsed = _parse_json(raw_responses)
            if isinstance(parsed, list):
                responses = parsed
        except json.JSONDecodeError:
            logger.warning("revise RESPONSES 块解析失败，忽略: %r", raw_responses[:120])
    return {
        "draft": draft,
        "revision_count": state.get("revision_count", 0) + 1,
        "revise_responses": responses,
    }


# ---- persist（编排层，§6.2 数据流边界的落库点）----

def node_persist(state: ChapterState) -> ChapterState:
    """确认分流（§6.11）：无 critical/L2 major → 低风险自动放行落库；有 → 候选池待人工。

    L2 major（正文-台账语义矛盾，§8.6 点级）与 L1 critical 同等待遇：语义不自洽的
    候选不自洽静默 auto 落库，进待确认池 + 章节 awaiting_review。
    """
    pid = state["project_id"]
    chapter_seq = state["chapter_seq"]
    candidates = state.get("candidates", [])
    report = state.get("report") or {}
    critical = report.get("summary", {}).get("critical", 0) or 0
    l2_major = report.get("summary", {}).get("l2_major", 0) or 0
    # 有限修订后仍未通过语义审核，必须停下；不能把预算耗尽当作内容合格。
    audit_failed = (state.get("audit_verdict") or {}).get("verdict") in ("rewrite", "replan")
    # 空名卡片是噪声（确认后什么也不做，_persist_candidates 会跳过）：不触发暂停、
    # 不进池（审计 Low-4）。
    has_card = any(
        c["kind"] == "character_card"
        and str((c.get("payload") or {}).get("name") or "").strip()
        for c in candidates
    )

    if critical or l2_major or has_card or audit_failed or state.get("needs_review"):
        # critical/L2 major / 新人物卡片 暂停：候选进待确认池，章节标 awaiting_review
        # （§6.11 / §7.11 ④——新人物是高风险 canon，必须人工确认后才落卡）。
        # plotline 推进 / 新设定实体 new_entity / 伏笔回收 foreshadow_touch 不落池：
        # 正文已写即推进已发生（低风险自动生效，§7.11 ④ 地点/物品/技能自动建档口径）；
        # plotline 连 CHECK 都不含，foreshadow_touch 是状态推进不改 canon。
        with tenant_session(pid) as db:
            auto_candidates = [c for c in candidates
                               if c["kind"] in ("new_entity", "foreshadow_touch")]
            if auto_candidates:
                _persist_candidates(db, pid, chapter_seq, auto_candidates,
                                    skip_pool_handled=False)
            for cand in candidates:
                if cand["kind"] in ("plotline", "new_entity", "foreshadow_touch"):
                    continue
                if cand["kind"] == "character_card" \
                        and not str((cand.get("payload") or {}).get("name") or "").strip():
                    continue
                # 幂等守卫：resume 续跑会重跑 extract → 防同 payload 候选在池里堆重复
                # （确认流：人工处理候选后 resume 重跑，重复项不应再进池）。
                if _pool_has_duplicate(db, pid, chapter_seq, cand):
                    continue
                db.add(MemoryCandidate(project_id=uuid.UUID(pid), kind=cand["kind"],
                                       source_chapter=chapter_seq, payload=cand["payload"],
                                       confidence=cand.get("confidence", 0.0)))
            # 正文属于用户已经得到的生成结果，必须立刻可见；人工确认只决定这些设定
            # 是否进入长期记忆。待确认稿同样保存内容和启发式摘要，刷新/重进后仍可读取。
            draft = state.get("draft")
            if draft:
                summary = _chapter_summary(state.get("summary_candidates") or candidates)
                repo.save_review_draft(
                    db, project_id=uuid.UUID(pid), chapter_seq=chapter_seq,
                    content=draft, summary=summary,
                )
            else:
                # 防御旧 checkpoint/测试夹具缺少 draft：仍保留待确认状态，不能整条任务崩溃。
                ch = repo.get_chapter(db, uuid.UUID(pid), chapter_seq)
                if ch:
                    ch.status = "awaiting_review"
                else:
                    db.add(Chapter(project_id=uuid.UUID(pid), chapter_seq=chapter_seq,
                                   status="awaiting_review"))
            # 运行记录写进同一事务（在会话内，否则会话关闭后 add 静默丢失，审计 Low-5）
            record_plain(db, project_id=pid, task_id=state.get("task_id"), node="persist",
                         detail={"candidates": len(candidates),
                                 "status": "awaiting_review",
                                 "reason": "critical" if critical else
                                           "l2_major" if l2_major else
                                           "audit_failed" if audit_failed else "character_card"})
        return {"persisted": True, "needs_review": True}

    with tenant_session(pid) as db:
        candidates = state.get("candidates", [])
        invalidation = None
        if state.get("rewrite"):
            # 显式重写（§7.3 失效重建，阶段 3）：先失效该章旧记忆（facts/states/relations
            # 时间窗关闭 + events/foreshadows/embeddings 删除），再写新记忆——同事务原子，
            # 新写失败回滚则旧记忆不失效。池候选重放：重写续跑补已确认候选、排除已拒绝
            # 候选，防「已确认记忆被失效后又因 skip_pool_handled 不重写」而丢失。
            if not state.get("review_revision"):
                candidates = _reapply_pool_for_rewrite(db, pid, chapter_seq, candidates)
            invalidation = invalidate_chapter_memory(db, project_id=uuid.UUID(pid),
                                                     chapter_seq=chapter_seq)
            _persist_candidates(db, pid, chapter_seq, candidates, skip_pool_handled=False)
        else:
            # auto 放行路径：跳过已进确认池的候选（评审 A2——critical→confirm→resume 后
            # 重跑无 critical 时，confirmed/rejected 候选不再重复落库）
            _persist_candidates(db, pid, chapter_seq, candidates, skip_pool_handled=True)
        # 落章节正文。summary 取事件候选摘要拼接；finalize 收尾时完整候选走
        # summary_candidates 键（掩码后 candidates 只剩 plotline，直接取会丢事件摘要）
        summary = _chapter_summary(state.get("summary_candidates") or candidates)
        repo.save_chapter(db, project_id=uuid.UUID(pid), chapter_seq=chapter_seq,
                          content=state["draft"], summary=summary, generation_source="auto")
        _advance_current_chapter(db, pid, chapter_seq)
        record_plain(db, project_id=pid, task_id=state.get("task_id"), node="persist",
                     detail={"candidates": len(candidates), "status": "auto_confirm",
                             "rewrite": bool(state.get("rewrite")), "invalidation": invalidation})
    return {"persisted": True, "needs_review": False}


def node_summarize(state: ChapterState) -> ChapterState:
    """章节摘要（§7 短期记忆）：落库后追加 LLM 真摘要，非阻塞——失败保留启发式摘要。

    追加节点（persist → summarize）：auto 路径 persist 已确认落库，这里以整章正文为输入
    生成 80-150 字摘要覆盖启发式；awaiting_review 首跑状态未 confirmed → 短路不产
    （确认流收尾时由 runner.finalize_chapter_review 单独补调）。LLM 失败/解析失败/异常
    只记 warning 返回空更新，绝不影响主链路。
    """
    pid = state["project_id"]
    seq = state["chapter_seq"]
    draft = state.get("draft")
    if not draft:
        return {}
    try:
        with tenant_session(pid) as db:
            ch = repo.get_chapter(db, uuid.UUID(pid), seq)
            if ch is None or ch.status != "confirmed":
                return {}  # 非落库终态（awaiting_review 首跑等）不产摘要
            messages = prompts.summary_messages(draft, seq)
            resp, _ = _llm(db, state, "summarize", "Summarize",
                           make_chain("summarize", db=db, project_id=pid), messages,
                           json_mode=True)
            if resp.error:
                logger.warning("章节摘要生成失败（保留启发式）: %s", resp.error)
                return {}
            data = _parse_json(resp.content)
            text = (data.get("summary") or "").strip() if isinstance(data, dict) else ""
            if not text:
                return {}
            ch.summary = text
    except Exception as exc:
        logger.warning("章节摘要生成失败（保留启发式）: %s", exc)
        return {}
    return {}


def finalize_awaiting_review(values: dict, *, task_id: str) -> dict:
    """确认流收尾（§6.11）：把 checkpoint 草稿落库为正文，不走图重跑。

    修复（2026-08-17）：LangGraph 1.2.9 的 invoke-resume 语义是「整图从 START 重跑」，
    不是断点续跑。确认流 resume 若走 resume_thread 重跑整章，确定性输入会再次命中
    critical/L2 major/新人物卡片 → 永久 awaiting_review、正文永不落库（用户实测：
    确认后点续跑变成重新生成）。故 await/review 章的续跑不重跑图，而是取 checkpoint
    状态（含 write 产出的草稿），掩码 report 走 node_persist auto 路径落库正文 + 推进进度。

    记忆已在确认流分头处理：confirmed 候选 confirm 时落库、未处理候选留池待后续评审、
    new_entity 首跑即建（低风险自动建档）、plotline 正文已写即推进——收尾只需把仍
    未落库的 plotline 补齐推进，其余不再重复写（skip_pool_handled + 池判重幂等兜底）。
    values 为 checkpoint 状态（snap.values），调用方负责从 checkpointer 取。
    """
    full_candidates = values.get("candidates") or []
    candidates = [c for c in full_candidates if c.get("kind") == "plotline"]
    return node_persist({
        **values,
        "task_id": task_id,
        "candidates": candidates,
        # 摘要兜底：掩码后 candidates 只剩 plotline，事件候选摘要会丢 → 完整原候选
        # 走 summary_candidates 键，node_persist 落库时取它拼章节摘要（回归修复）
        "summary_candidates": full_candidates,
        "report": {"summary": {"critical": 0, "l2_major": 0}},  # 掩码：跳过确认分流
        "audit_verdict": None,  # 作者明确续跑接受当前稿，避免再次停在同一语义审核
        "needs_review": False,
    })


def _persist_candidates(db: Session, pid: str, chapter_seq: int, candidates: list[dict],
                        *, skip_pool_handled: bool = False) -> None:
    """候选落库（auto 放行 / confirm 共用，§6.11）。

    skip_pool_handled=True（auto 路径，评审 A2）：候选已进过确认池（confirm 已落库 /
    reject 已拒绝）则跳过，防止「critical→confirm→resume→无 critical」时重复写入记忆。
    confirm_candidate 调用传 False（正在确认的那条必须落库）。"""
    project_id = uuid.UUID(pid)
    for cand in candidates:
        # plotline / foreshadow_touch 不落确认池（低风险自动生效），无需查池
        if skip_pool_handled and cand["kind"] not in ("plotline", "foreshadow_touch") \
                and _pool_has_duplicate(db, pid, chapter_seq, cand):
            continue
        p = cand.get("payload") or {}
        if cand["kind"] == "event":
            # participants 是人物名（extract LLM 输出）→ 归一化为 canonical id（§7.5）
            participants: list[uuid.UUID] = []
            for name in p.get("participants", []):
                cid = _resolve_character_id(db, pid, str(name))
                if cid:
                    participants.append(cid)
            ev = Event(project_id=project_id, summary=p.get("summary", ""),
                       participants=[str(c) for c in participants],
                       source_chapter=chapter_seq, confidence=p.get("confidence", 0.8))
            db.add(ev)
            db.flush()  # 拿 ev.id 供向量索引关联
            _index_event_embedding(db, project_id, ev)
        elif cand["kind"] == "character_state":
            character_id = uuid.UUID(str(p["character_id"]))
            field = str(p["field"])
            current = repo.get_character_state(
                db, project_id, character_id, max(0, chapter_seq - 1)
            ).get(field)
            old_value = current if current is not None else p.get("old_value")
            new_value = _materialize_cumulative_state(field, old_value, p.get("new_value"))
            db.add(CharacterState(project_id=project_id,
                                  character_id=character_id,
                                  chapter_seq=chapter_seq, field=field,
                                  old_value=old_value, new_value=new_value,
                                  source_chapter=chapter_seq, confidence=p.get("confidence", 0.8)))
        elif cand["kind"] == "fact":
            fact = Fact(project_id=project_id, content=p.get("content", ""),
                        category=p.get("category"), is_hard=bool(p.get("is_hard")),
                        source_chapter=chapter_seq, confidence=p.get("confidence", 0.8),
                        confirm_status="confirmed")
            db.add(fact)
            db.flush()  # 拿 fact.id 供向量索引关联
            _index_fact_embedding(db, project_id, fact)
        elif cand["kind"] == "relation_change":
            # 关系写入语义（§3 决策）：落库前先关闭同 (source_id, target_id) 有序对全部活跃
            # 旧行（schema.md §6「当前关系 = 最新 valid_to IS NULL」），透传候选 valid_to（临时盟约）。
            # 双端缺一或类型缺失无法定位关系对 → 跳过且不关任何行（§6.12 坏数据拒绝但不崩）。
            src = p.get("source_id")
            tgt = p.get("target_id")
            rtype = p.get("relation_type")
            if src and tgt and rtype:
                src_uuid, tgt_uuid = uuid.UUID(str(src)), uuid.UUID(str(tgt))
                _close_active_relations(db, project_id, src_uuid, tgt_uuid, chapter_seq)
                db.add(Relation(project_id=project_id, source_id=src_uuid,
                                relation_type=rtype, target_id=tgt_uuid,
                                confidence=p.get("confidence", 0.8), source_chapter=chapter_seq,
                                valid_from=p.get("valid_from", 1),
                                valid_to=p.get("valid_to")))
        elif cand["kind"] == "character_card":
            # 新人物卡片（§7.11 ④）：确认后建 characters 静态基底 + alias 归一化（§7.5）。
            # 同名/同别名已建档跳过——防重复建卡（池内同 payload 去重是章级，跨章靠这里，
            # get_character 已接别名解析）。
            name = str(p.get("name") or "").strip()
            personality = str(p.get("personality") or "").strip() or None
            if name and repo.get_character(db, project_id, name) is None:
                char = Character(project_id=project_id, name=name, realm_cap="无",
                                 personality=personality,
                                 base_attrs={k: p.get(k) for k in ("identity", "role", "importance")
                                             if p.get(k) is not None and str(p.get(k)).strip()})
                db.add(char)
                db.flush()  # 拿 char.id 供 alias 关联
                db.add(Alias(project_id=project_id, alias=name, entity_id=char.id))
        elif cand["kind"] == "new_entity":
            # 新设定实体（§7.11 ④ 武器/功法/技能/地点自动建档）：低风险登记，同名去重。
            name = str(p.get("name") or "").strip()
            etype = str(p.get("entity_type") or "").strip()
            # 白名单（§6.12 坏数据拒绝但不崩）：LLM 输出白名单外类型（如「神器」）不登记
            # ——否则落库后设定页三桶渲染不可见（审计 Low-3）。
            if name and etype in ("item", "skill", "location") \
                    and _entity_exists(db, project_id, etype, name) is None:
                desc = str(p.get("description") or "").strip() or None
                ent = Entity(project_id=project_id, entity_type=etype, canonical_name=name,
                             properties={"description": desc,
                                         "first_seen_chapter": chapter_seq})
                db.add(ent)
                db.flush()
                db.add(Alias(project_id=project_id, alias=name, entity_id=ent.id))
                # 地点实体镜像进 Location 注册表（§9 图谱地点层级数据源）：new_entity
                # 只写 Entity 时图谱地点全是孤立点——建/并 Location 行并解析 parent 上级，
                # 让自动建档地点也能挂出 hierarchy 边（world_graph 读 Location.parent_id）。
                if etype == "location":
                    _upsert_location(db, project_id, name,
                                     parent_name=str(p.get("parent") or "").strip() or None)
        elif cand["kind"] == "foreshadow":
            db.add(Foreshadow(project_id=project_id, description=p.get("description", ""),
                              status="planted", planted_chapter=chapter_seq, trigger=p.get("trigger") or {}))
        elif cand["kind"] == "foreshadow_touch":
            # 伏笔自动回收（§7.9 抽取驱动）：id 在项目内存在才推进/解决，防幻觉 id 落库。
            # 缺 id / 查不到 → 丢弃（extract 层已尽力，persist 兜底防炸批次；RLS 隔离租户）。
            try:
                fs_id = uuid.UUID(str(p.get("foreshadow_id") or ""))
            except (ValueError, TypeError):
                continue
            fs = db.get(Foreshadow, fs_id)
            if fs is None:
                continue
            outcome = str(p.get("outcome") or "")
            if outcome == "resolved":
                if fs.status != "resolved":
                    fs.status = "resolved"
                    fs.resolved_chapter = chapter_seq
                fs.last_touched = chapter_seq
            elif outcome == "advanced" and fs.status != "resolved":
                if fs.status == "planted":
                    fs.status = "developing"
                fs.last_touched = chapter_seq
        elif cand["kind"] == "plotline":
            # 剧情线推进台账（§7.9）：正文已写 = 推进已发生，低风险自动生效（同 §7.11
            # 地点自动建档），不落候选池。按名称匹配线程更新最近推进章；匹配不到不推进。
            name = str(p.get("thread_name", "") or "").strip()
            if name:
                for t in repo.get_plot_threads(db, project_id):
                    if name == (t.name or "") or name in (t.name or "") or (t.name or "") in name:
                        t.last_progress_chapter = chapter_seq
                        break


def _entity_exists(db: Session, project_id: uuid.UUID, entity_type: str,
                   name: str) -> Entity | None:
    """同名同类型设定实体去重（§7.11 ④ 自动建档：已登记不重复建）。

    canonical 名精确命中优先；未命中 → 回退别名解析（§7.5：正文以别名出现时归一到
    同一实体）。别名 polymorphic，校验 entity_id 确是本项目该类型实体。
    """
    ent = db.query(Entity).filter(
        Entity.project_id == project_id,
        Entity.entity_type == entity_type,
        Entity.canonical_name == name,
    ).first()
    if ent:
        return ent
    alias_row = db.query(Alias).filter(
        Alias.project_id == project_id, Alias.alias == name
    ).first()
    if alias_row:
        ent = db.get(Entity, alias_row.entity_id)
        if ent is not None and ent.project_id == project_id \
                and ent.entity_type == entity_type:
            return ent
    return None


def _upsert_location(db: Session, project_id: uuid.UUID, name: str, *,
                     parent_name: str | None = None) -> None:
    """地点实体镜像进 Location 注册表（§9 图谱地点层级数据源，同名 create-if-missing）。

    parent_name 非空时解析上级地点（同名建行，保证 hierarchy 边两端都在节点集内），
    写入 parent_id。幂等：同名已存在则复用行、只补 parent_id（不覆盖旧行其它字段）。
    """
    row = db.query(Location).filter(Location.project_id == project_id,
                                    Location.name == name).first()
    if row is None:
        row = Location(project_id=project_id, name=name)
        db.add(row)
        db.flush()
    if parent_name:
        parent = db.query(Location).filter(Location.project_id == project_id,
                                           Location.name == parent_name).first()
        if parent is None:
            parent = Location(project_id=project_id, name=parent_name)
            db.add(parent)
            db.flush()
        if row.parent_id != parent.id:
            row.parent_id = parent.id


def _close_active_relations(db: Session, project_id: uuid.UUID, source_id: uuid.UUID,
                            target_id: uuid.UUID, chapter_seq: int) -> int:
    """收口同一 (source_id, target_id) 有序对的全部活跃旧关系（valid_to = 本章序，§7.8）。

    维护 schema.md §6「当前关系 = 最新一条 valid_to IS NULL」：新关系变更落库前必须把
    该对旧活跃行全部关闭，保证「每对至多一条活跃」不变量。跨类型全关（按对，非按类型）；
    只关同向对，不代写反向 (target, source)（§9.3 成对落库语义由写入侧负责）。返回关闭行数。
    """
    rows = db.query(Relation).filter(
        Relation.project_id == project_id,
        Relation.source_id == source_id,
        Relation.target_id == target_id,
        Relation.valid_to.is_(None),
    ).all()
    for r in rows:
        r.valid_to = chapter_seq
    return len(rows)


def _pool_has_duplicate(db: Session, pid: str, chapter_seq: int, cand: dict) -> bool:
    """候选池去重：同 kind+payload+章 的候选已存在（任意状态）则跳过。

    评审 A2 修复：原只查 pending——人工 confirm/reject 后状态变 confirmed/rejected，
    resume 重跑 extract 时对旧实现不可见 → 同 payload 候选再次进池 / 再次落库。
    现在任意状态（pending/confirmed/rejected）都挡重写，池内 (kind, payload, 章)
    至多一条。payload 列是 `JSON`（PG json 无 `=` 运算符），故拉同 kind+章的候选、
    Python 侧比对字典（池单章量级小，一次查询可接受）。"""
    from sqlalchemy import select

    project_id = uuid.UUID(pid)
    rows = db.execute(
        select(MemoryCandidate).where(
            MemoryCandidate.project_id == project_id,
            MemoryCandidate.kind == cand["kind"],
            MemoryCandidate.source_chapter == chapter_seq,
        )
    ).scalars().all()
    return any(r.payload == cand["payload"] and not (r.review or {}).get("superseded") for r in rows)


def _reapply_pool_for_rewrite(db: Session, pid: str, chapter_seq: int,
                              candidates: list[dict]) -> list[dict]:
    """重写续跑池候选重放（§7.3 失效重建）：补已确认、排除已拒绝。

    重写会先失效该章旧记忆；若原章曾走 critical→confirm→resume，池内有已确认候选
    （其落库记忆会被失效）——重写后必须重放这些确认项，否则用户确认的记忆丢失。
    rejected 语义保留：用户拒过的候选不因重写复活。pending 候选不动（仍留池待处理）。
    按 (kind, payload) 与 state 候选去重（复用 _pool_has_duplicate 比对口径）。
    """
    from sqlalchemy import select

    project_id = uuid.UUID(pid)
    pool = db.execute(
        select(MemoryCandidate).where(
            MemoryCandidate.project_id == project_id,
            MemoryCandidate.source_chapter == chapter_seq,
        )
    ).scalars().all()
    pool_by_status: dict[str, list[dict]] = {}
    for row in pool:
        pool_by_status.setdefault(row.status, []).append(
            {"kind": row.kind, "payload": row.payload, "confidence": row.confidence})

    # 1) state 候选排除池内已拒绝项（rejected 语义保留）
    result = []
    for cand in candidates:
        if any(r["kind"] == cand["kind"] and r["payload"] == cand["payload"]
               for r in pool_by_status.get("rejected", [])):
            continue
        result.append(cand)

    # 2) 补入池内已确认且 state 未再产出的项（防确认记忆在失效后被丢）
    for conf in pool_by_status.get("confirmed", []):
        if not any(c["kind"] == conf["kind"] and c["payload"] == conf["payload"] for c in result):
            result.append(conf)
    return result


def confirm_candidate(db: Session, project_id: str, candidate_id: uuid.UUID) -> MemoryCandidate | None:
    """人工确认待确认池候选落库（§6.11 确认分流 / §7.3 事实生命周期）。

    编排层写库入口：候选的 kind+payload 走与 persist 自动放行完全相同的落库路径
    （_persist_candidates，含人物名归一化 / 向量索引降级），候选标 confirmed。
    归属断言：tenant_session 已按 project_id RLS 隔离，此处再显式校验 project_id
    防歧义（双保险，§14.1）。非 pending 候选（已确认/已拒绝）返回 None 不可重复处理。
    """
    cand = db.get(MemoryCandidate, candidate_id)
    if cand is None or str(cand.project_id) != project_id or cand.status != "pending":
        return None
    if cand.kind == "memory_removal":
        # 删除候选（阶段 3 编辑校正）：确认 = 按类型失效被删记忆，而非写新记忆。
        # 幂等：目标记忆已不存在视为已删除 → True；payload 非法返回 False → 409。
        if not apply_memory_removal(db, project_id=uuid.UUID(project_id),
                                    payload=cand.payload, chapter_seq=cand.source_chapter):
            return None
    else:
        _persist_candidates(db, project_id, cand.source_chapter,
                            [{"kind": cand.kind, "payload": cand.payload, "confidence": cand.confidence}])
    cand.status = "confirmed"
    return cand


def _index_embedding(db: Session, *, project_id: uuid.UUID, level: str,
                     source_id: uuid.UUID, source_chapter: int | None,
                     text: str, model_version: str = "bge-m3") -> None:
    """通用向量化入 embeddings（分层 event/world/chapter，§7.2/§15）。

    加分项：bge-m3 未装/加载失败都降级（记录日志不阻塞落库，§6.12 数据层），
    伏笔/人设走偏主防线是关系链路（foreshadows 状态机 + 台账），不依赖本函数。
    """
    if not text:
        return
    try:
        emb = get_embedder().encode([text])[0]
        PgvectorStore().upsert(db, project_id=project_id, level=level, source_id=source_id,
                               source_chapter=source_chapter,
                               model_version=model_version, embedding=emb)
    except Exception as exc:
        logger.warning("向量化失败（level=%s），跳过索引（不影响落库）: %s", level, exc)


def _index_event_embedding(db: Session, pid: str, ev: Event) -> None:
    """事件摘要向量化入 embeddings（level=event，§15 最小向量召回）。"""
    _index_embedding(db, project_id=ev.project_id, level="event", source_id=ev.id,
                     source_chapter=ev.source_chapter, text=ev.summary)


def _index_fact_embedding(db: Session, pid: str, fact: Fact) -> None:
    """世界观/长期事实向量化入 embeddings（level=world，§7.2 分层 collection）。

    硬约束恒在 Top-K、不参与相似度截断（§7.2），向量化是浪费 → 跳过。
    """
    if fact.is_hard:
        return
    _index_embedding(db, project_id=fact.project_id, level="world", source_id=fact.id,
                     source_chapter=fact.source_chapter, text=fact.content)


def _chapter_summary(candidates: list[dict]) -> str:
    return "；".join(p.get("summary", "") for c in candidates if c["kind"] == "event" for p in [c.get("payload") or {}] if p.get("summary"))


def _advance_current_chapter(db: Session, pid: str, chapter_seq: int) -> None:
    """推进书当前进度 `Project.current_chapter`（§11.1，展示「下一章」与写保护基准）。

    之前恒为 0（没人更新），导致前端默认序号永远是 1、重复重写第 1 章。
    落库时置为 max(current, seq)——单调递增，不因重写已写章倒退。
    """
    from aiink.models import Project

    project_id = uuid.UUID(pid)
    proj = db.get(Project, project_id)
    if proj is not None and chapter_seq > (proj.current_chapter or 0):
        proj.current_chapter = chapter_seq


# ---- reflexion 复盘沉淀（§8.9）：audit findings → 跨章写作经验 ----

_SEVERITY_RANK = {"critical": 4, "major": 3, "minor": 2, "hint": 1}


def _collect_batch_audit_findings(db: Session, batch_task_id: str) -> list[dict]:
    """整批各章 audit findings（唯一持久化点 = agent_runs.detail，§6.8）。

    每章取最后一条 audit 行（settled 终态——不把 rewrite 循环里已修的发现重复灌入，
    后行覆盖即得本章最终 verdict）。每条补 `_chapter`（从 task_id `{batch}:ch{seq}` 解析）。
    """
    runs = db.query(AgentRun).filter(
        AgentRun.task_id.like(f"{batch_task_id}:ch%"), AgentRun.node == "audit",
    ).order_by(AgentRun.id.asc()).all()
    final: dict[str, AgentRun] = {}
    for r in runs:
        final[r.task_id] = r
    out: list[dict] = []
    for tid, r in final.items():
        verdict = (r.detail or {}).get("audit_verdict") or {}
        ch = int(tid.rsplit("ch", 1)[1])
        for f in verdict.get("findings") or []:
            f = dict(f)
            f["_chapter"] = ch
            out.append(f)
    return out


def _update_recurrences(db: Session, project_id: str, findings: list[dict],
                        start_chapter: int) -> int:
    """复发记账（确定性，不耗 LLM）：新 finding.conflict_type == active lesson.category
    且 lesson.source_chapter < 本批首章 → 复发。按 (lesson, chapter) 去重（rewrite 循环内
    同一 finding 只记一次）。返回本次复发数。"""
    pid = uuid.UUID(project_id)
    active = db.query(WritingLesson).filter(
        WritingLesson.project_id == pid, WritingLesson.status == "active"
    ).all()
    by_cat = {l.category: l for l in active}
    seen: set[tuple[str, int]] = set()
    for f in findings:
        lesson = by_cat.get(f.get("conflict_type"))
        if not lesson or (lesson.source_chapter or 0) >= start_chapter:
            continue
        seen.add((str(lesson.id), f.get("_chapter") or 0))
    for lid, ch in seen:
        lesson = next(l for l in active if str(l.id) == lid)
        lesson.recurrence_count = (lesson.recurrence_count or 0) + 1
        lesson.last_recurrence_at = max(lesson.last_recurrence_at or 0, ch)
    db.flush()
    return len(seen)


def _batch_already_reflexed(db: Session, project_id: str, batch_task_id: str) -> bool:
    """同批次已提炼过 → 跳过（幂等 guard，重跑不堆重复）。"""
    return db.query(WritingLesson).filter(
        WritingLesson.project_id == uuid.UUID(project_id),
        WritingLesson.source_batch_task_id == batch_task_id,
    ).first() is not None


def _covered_by_active(db: Session, project_id: str, findings: list[dict],
                       start_chapter: int) -> list[dict]:
    """已被在效经验覆盖的发现（同类经验 source_chapter < 本批首章）：不发 LLM、不新增经验。

    复发率已在 _update_recurrences 记账；只把「未被覆盖」的新发现喂 LLM 提炼/演化。
    """
    pid = uuid.UUID(project_id)
    active = db.query(WritingLesson).filter(
        WritingLesson.project_id == pid, WritingLesson.status == "active"
    ).all()
    covered_cats = {l.category for l in active if (l.source_chapter or 0) < start_chapter}
    return [f for f in findings if f.get("conflict_type") not in covered_cats]


def _persist_lessons(db: Session, project_id: str, batch_task_id: str, start: int,
                     lessons: list[dict], findings: list[dict]) -> tuple[int, int]:
    """提炼产物落库（编排层，§6.2 数据流边界）：Agent 不直写。

    - 可溯源守卫：lesson.conflict_type 必须在 findings 里有同型发现，否则丢弃（LLM 幻觉）；
    - 路由：同 category 已有行 → update 演化（保留 id、继承复发指标）；无 → create；
    - 分级（仅 create）：同型 findings 最高 severity critical/major → proposed，否则 active；
    - 去重：content_hash 字面（同 project+content_hash 任意状态已存在 → 跳过）。
    返回 (inserted, skipped_duplicates)。
    """
    pid = uuid.UUID(project_id)
    by_type: dict[str, list[dict]] = {}
    for f in findings:
        by_type.setdefault(f.get("conflict_type"), []).append(f)
    existing = {l.category: l for l in db.query(WritingLesson).filter(
        WritingLesson.project_id == pid).all()}
    inserted = skipped = 0
    for lesson in lessons:
        src = by_type.get(lesson.get("conflict_type")) or []
        if not src:
            continue  # 不可溯源 → 丢弃
        max_sev = max((f.get("severity") for f in src), key=lambda s: _SEVERITY_RANK.get(s, 0))
        h = hashlib.sha256(lesson["content"].encode("utf-8")).hexdigest()
        dup = db.query(WritingLesson).filter(
            WritingLesson.project_id == pid, WritingLesson.content_hash == h).first()
        if dup:
            skipped += 1
            continue
        evidence = [{k: f.get(k) for k in ("chapter", "conflict_type", "severity", "quote", "suggestion")}
                    for f in src if isinstance(f, dict)]
        src_ch = min((f.get("_chapter") or start for f in src), default=start)
        prev = existing.get(lesson.get("conflict_type"))
        if prev is not None:
            # 总结演化：同 category 更新同一行——content 换演化版、evidence 追加、保留 id 与复发指标
            prev.content = lesson["content"]
            prev.content_hash = h
            prev.confidence = lesson.get("confidence") or prev.confidence
            merged = prev.evidence or []
            existing_keys = {(e.get("chapter"), e.get("quote")) for e in merged if isinstance(e, dict)}
            for e in evidence:
                if (e.get("chapter"), e.get("quote")) not in existing_keys:
                    merged.append(e)
            prev.evidence = merged
            prev.source_chapter = min(prev.source_chapter or start, src_ch)
            prev.lesson_type = lesson.get("lesson_type", prev.lesson_type or "both")
            prev.source_batch_task_id = batch_task_id
            inserted += 1
            continue
        status = "proposed" if max_sev in ("critical", "major") else "active"
        db.add(WritingLesson(
            project_id=pid, category=lesson["conflict_type"],
            lesson_type=lesson.get("lesson_type", "both"),
            content=lesson["content"], content_hash=h,
            evidence=evidence, confidence=lesson.get("confidence") or 1.0,
            source_chapter=src_ch, source_batch_task_id=batch_task_id, status=status,
        ))
        inserted += 1
    db.flush()
    return inserted, skipped


def _collect_chapter_window_findings(db: Session, project_id: str,
                                     start_chapter: int, end_chapter: int) -> list[dict]:
    """单章流窗口内各章 audit findings（§8.9 扩展）。

    合并单章任务与批次子线程的 settled findings（每 task_id 取最后一条 audit 行，
    同 _collect_batch_audit_findings 口径——不把 rewrite 循环里已修的发现重复灌入）。
    章号映射：批次子线程 task_id `{batch}:ch{seq}` → seq；单章任务 → Task.chapter_seq。
    逐条补 `_chapter` 供复发记账 / 溯源。批次主任务 chapter_seq 为空自然跳过。
    """
    from aiink.models import Task

    pid = uuid.UUID(project_id)
    runs = db.query(AgentRun).filter(
        AgentRun.project_id == pid, AgentRun.node == "audit",
    ).order_by(AgentRun.id.asc()).all()
    single_ids = [r.task_id for r in runs if r.task_id and ":ch" not in r.task_id]
    seq_by_task: dict[str, int] = {}
    if single_ids:
        rows = db.query(Task.id, Task.chapter_seq).filter(
            Task.project_id == pid, Task.id.in_([uuid.UUID(t) for t in single_ids]),
            Task.chapter_seq.isnot(None)).all()
        seq_by_task = {str(tid): int(seq) for tid, seq in rows if seq is not None}
    final: dict[str, AgentRun] = {}
    for r in runs:
        final[r.task_id] = r
    out: list[dict] = []
    for tid, r in final.items():
        if tid and ":ch" in tid:
            try:
                ch = int(tid.rsplit("ch", 1)[1])
            except ValueError:
                continue
        else:
            ch = seq_by_task.get(tid)
        if not ch or not (start_chapter <= ch <= end_chapter):
            continue
        verdict = (r.detail or {}).get("audit_verdict") or {}
        for f in verdict.get("findings") or []:
            f = dict(f)
            f["_chapter"] = ch
            out.append(f)
    return out


def reflexion_for_chapter_window(*, project_id: str, end_chapter: int, task_id: str) -> dict:
    """单章流每 N 章复盘（§8.9 扩展）：窗口 [end-N+1, end] 内 audit findings → 复发记账 →
    LLM 总结演化写作经验。复盘是加分项，任何失败只记 error 不抛（worker 调用方兜底 try）。

    batch 流走 batch_end node_reflexion 不走这里；此处只服务单章流（chapter_generate /
    chapter_resume）。source_batch_task_id 记本次触发的任务 id，作经验溯源。
    """
    start = max(1, end_chapter - settings.chapter_reflexion_interval + 1)
    try:
        with tenant_session(project_id) as db:
            findings = _collect_chapter_window_findings(db, project_id, start, end_chapter)
            recurrences = _update_recurrences(db, project_id, findings, start)
            new_findings = _covered_by_active(db, project_id, findings, start)
            if not new_findings:
                record_plain(db, project_id=project_id, task_id=task_id, node="reflexion",
                             detail={"findings": len(findings), "recurrences": recurrences,
                                     "lessons": 0, "reason": "no_new_findings"})
                return {"reflexion": {"findings": len(findings), "recurrences": recurrences,
                                      "lessons": 0, "reason": "no_new_findings"}}
            existing = repo.get_active_lessons(db, uuid.UUID(project_id))
            messages = prompts.reflexion_messages(new_findings, [
                {"category": l.category, "content": l.content, "recurrence_count": l.recurrence_count}
                for l in existing
            ], start, settings.chapter_reflexion_interval)
            resp = make_chain("audit", db=db, project_id=project_id).generate(messages, json_mode=True,
                                                max_tokens=_MAX_TOKENS["reflexion"])
            record_run(db, project_id=project_id, task_id=task_id, node="reflexion",
                       role="Reflexion", resp=resp, error=resp.error,
                       detail={"findings": len(findings), "recurrences": recurrences})
            if resp.error:
                return {"reflexion": {"error": resp.error}}
            data = _parse_json(resp.content)
            lessons = data.get("lessons", []) if isinstance(data, dict) else []
            inserted, skipped = _persist_lessons(db, project_id, task_id, start, lessons, new_findings)
            return {"reflexion": {"findings": len(findings), "recurrences": recurrences,
                                  "lessons": inserted, "skipped_duplicates": skipped}}
    except Exception as exc:
        logger.warning("章节窗口复盘失败（不阻塞任务）: %s", exc)
        return {"reflexion": {"error": str(exc)}}


def confirm_lesson(db: Session, project_id: str, lesson_id: uuid.UUID) -> WritingLesson | None:
    """人工确认经验生效（proposed→active，§8.9）。幂等：非 proposed 返回 None。

    归属断言同 confirm_candidate：tenant_session RLS + 显式 project_id 比对（双保险，§14.1）。
    """
    lesson = db.get(WritingLesson, lesson_id)
    if lesson is None or str(lesson.project_id) != project_id or lesson.status != "proposed":
        return None
    lesson.status = "active"
    return lesson


def reject_lesson(db: Session, project_id: str, lesson_id: uuid.UUID) -> WritingLesson | None:
    """拒绝经验（proposed→rejected，§8.9）。幂等：非 proposed 返回 None。"""
    lesson = db.get(WritingLesson, lesson_id)
    if lesson is None or str(lesson.project_id) != project_id or lesson.status != "proposed":
        return None
    lesson.status = "rejected"
    return lesson
