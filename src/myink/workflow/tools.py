"""Agent 只读查证工具（§10：Audit/Writer 持只读工具，写库仍走编排层确认）。

数据流边界（§6.2）：本模块全部**只读**——不 import nodes、不碰写路径；写库只发生在
persist（编排层）。`project_id` 一律由调用方从 state 绑定传入 `execute_tool`，**绝不出现在
LLM 可写的 arguments 里**（即使 LLM 传了也忽略）——归属断言靠「参数绑定 + repo 显式
project_id + tenant_session RLS」三重。

工具结果统一 JSON 串（`default=str` 兜底时间戳/枚举），每工具行数 cap，控 token。
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy.orm import Session

from myink.memory import repository as repo

logger = logging.getLogger(__name__)

# 单工具结果行数上限（控上下文 token）
_MAX_ROWS = 20


def _tool(name: str, description: str, parameters: dict) -> dict:
    """OpenAI function calling 格式工具定义（DeepSeek 官方兼容）。"""
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": parameters}}


# 只读查证工具注册表（Audit/Writer 共用一套，无权限差异，§10）
TOOL_SCHEMAS: list[dict] = [
    _tool(
        "inspect_character",
        "查询某人物在指定章节时刻的静态档案与状态台账（境界/伤势/位置等，只读）。",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "人物名（如 林砚）"},
            "chapter_seq": {"type": "integer", "description": "状态物化到该章之前的台账（缺省用当前章）"},
        }, "required": ["name"]},
    ),
    _tool(
        "inspect_foreshadows",
        "查询当前开放伏笔列表（待回收的悬念/物件/承诺，按关键词过滤可留空，只读）。",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "可选关键词，过滤伏笔描述"},
        }, "required": []},
    ),
    _tool(
        "inspect_plot_threads",
        "查询活跃剧情线及其进度（哪条线在推进、最近推进到哪章，只读）。",
        {"type": "object", "properties": {}, "required": []},
    ),
    _tool(
        "inspect_facts",
        "查询已确认的世界观事实与硬约束（按关键词过滤可留空，只读）。",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "可选关键词，过滤事实内容"},
            "chapter_seq": {"type": "integer", "description": "按章号取当时有效的硬约束（缺省全部）"},
        }, "required": []},
    ),
]

# Audit/Writer 共用同一套（§10：全部只读、无权限差异）
READ_TOOLS: list[dict] = TOOL_SCHEMAS


def _to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---- 工具执行体（确定性只读，服务端绑定 project_id）----

def _inspect_character(db: Session, project_id: uuid.UUID, args: dict) -> dict:
    name = str(args.get("name", "")).strip()
    if not name:
        return {"error": "inspect_character 需要 name"}
    ch = repo.get_character(db, project_id, name)
    if not ch:
        return {"found": False, "name": name}
    chapter_seq = args.get("chapter_seq") or 0
    state = repo.get_character_state(db, project_id, ch.id, chapter_seq) if chapter_seq else {}
    return {
        "found": True, "name": ch.name, "realm_cap": ch.realm_cap,
        "personality": ch.personality, "state": state,
    }


def _inspect_foreshadows(db: Session, project_id: uuid.UUID, args: dict) -> dict:
    query = str(args.get("query", "") or "").strip()
    rows = repo.get_open_foreshadows(db, project_id)
    if query:
        rows = [f for f in rows if query in (f.description or "")]
    return {"count": len(rows[: _MAX_ROWS]), "foreshadows": [
        {"description": f.description, "status": f.status, "planted_chapter": f.planted_chapter,
         "trigger": f.trigger or {}} for f in rows[: _MAX_ROWS]
    ]}


def _inspect_plot_threads(db: Session, project_id: uuid.UUID, args: dict) -> dict:
    rows = repo.get_plot_threads(db, project_id)
    return {"count": len(rows[: _MAX_ROWS]), "threads": [
        {"name": t.name, "kind": t.kind, "status": t.status,
         "last_progress_chapter": t.last_progress_chapter} for t in rows[: _MAX_ROWS]
    ]}


def _inspect_facts(db: Session, project_id: uuid.UUID, args: dict) -> dict:
    query = str(args.get("query", "") or "").strip()
    rows = repo.get_hard_facts(db, project_id, args.get("chapter_seq"))
    if query:
        rows = [f for f in rows if query in (f.content or "")]
    return {"count": len(rows[: _MAX_ROWS]), "facts": [
        {"content": f.content, "category": f.category, "is_hard": f.is_hard,
         "source_chapter": f.source_chapter} for f in rows[: _MAX_ROWS]
    ]}


_EXECUTORS = {
    "inspect_character": _inspect_character,
    "inspect_foreshadows": _inspect_foreshadows,
    "inspect_plot_threads": _inspect_plot_threads,
    "inspect_facts": _inspect_facts,
}


def execute_tool(db: Session, project_id: uuid.UUID, name: str, arguments: dict) -> str:
    """执行一个只读查证工具，返回 JSON 串。

    设计：未知工具 / 参数错误 / 底层异常一律返回 `{"error": ...}` 而非抛出——
    让模型自行恢复，避免整个 tenant_session 回滚连带丢 agent_runs。project_id 只取
    调用方绑定实参（LLM 传的 project_id 一律忽略）。
    """
    try:
        handler = _EXECUTORS.get(name)
        if handler is None:
            return _to_json({"error": f"未知工具: {name}"})
        args = arguments or {}
        if not isinstance(args, dict):
            args = {}
        result = handler(db, project_id, args)
        return _to_json(result)
    except Exception as exc:
        logger.warning("工具 %s 执行失败: %s", name, exc)
        return _to_json({"error": f"工具 {name} 执行失败: {exc}"})


__all__ = ["TOOL_SCHEMAS", "READ_TOOLS", "execute_tool"]
