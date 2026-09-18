"""建书设定骨架草稿生成（§7.11 ②：一句话梗概启动 + Planner 提案 + 用户确认落库）。

与 style_extract.extract_style_profile 同款模式：planner 档一次 json_mode 调用 + 鲁棒
JSON 解析 + agent_runs 记录（§6.8 成本透明）+ 从不 raise（§6.12 降级，失败返回 ({}, error)）。

生成的是**可编辑草稿不落库**——用户逐项确认/修改后由 setup 端点落库（数据流边界 §6.2：
agent 只提案、确认 = 编排层写库入口）。草稿可再生（前端「重新生成」反复调用本函数）。
"""

from __future__ import annotations


def generate_book_setup(genre: str, premise: str, *,
                        genre_pack: dict | None = None,
                        project_id: str | None = None, db=None) -> tuple[dict, str | None]:
    """Planner 生成本书设定骨架草稿（境界体系/世界观/硬约束/势力/核心人物/关键地点）。

    返回 (draft, error)：LLM / 解析失败 → ({}, error)（§6.12 降级，端点回空草稿让用户手填）。
    函数内懒导入 providers/workflow（api 层 import 本模块时避免 import 环）。

    db 非 None 时记 agent_runs：generate 后立即 record_run（含降级行 error=resp.error），
    提交由调用方负责；此时 project_id 必填。
    """
    from myink.providers import make_chain
    from myink.workflow import nodes, prompts

    messages = prompts.book_setup_messages(genre, premise, genre_pack=genre_pack)
    resp = make_chain("planner", db=db, project_id=project_id).generate(
        messages, json_mode=True, max_tokens=nodes._MAX_TOKENS["book_setup"])
    if db is not None:
        if project_id is None:
            raise ValueError("db 非 None 时必须提供 project_id（agent_runs 归属）")
        nodes.record_run(db, project_id=project_id, task_id=None, node="book_setup",
                         role="Planner", resp=resp, error=resp.error, detail={"genre": genre})
    if resp.error:
        return {}, resp.error
    try:
        data = nodes._parse_json(resp.content)
    except Exception as exc:  # noqa: BLE001 —— 解析失败同 LLM 失败处理（§6.12）
        return {}, f"parse_error: {exc}"
    if not isinstance(data, dict):
        return {}, "unexpected_json"
    return data, None


def generate_book_outline(genre: str, premise: str, *, chapter_count: int = 200,
                          storyline: str = "", genre_pack: dict | None = None,
                          project_id: str | None = None,
                          db=None) -> tuple[dict, str | None]:
    """Planner 生成整书大纲草稿：Objective + 卷 + 约 30 章一段的阶段。草稿不落库。"""
    from myink.providers import make_chain
    from myink.workflow import nodes, prompts
    from myink.workflow.outline import normalize_outline

    messages = prompts.book_outline_messages(
        genre, premise, chapter_count, storyline, genre_pack=genre_pack)
    resp = make_chain("planner", db=db, project_id=project_id).generate(
        messages, json_mode=True, max_tokens=nodes._MAX_TOKENS["book_outline"])
    if db is not None:
        if project_id is None:
            raise ValueError("db 非 None 时必须提供 project_id（agent_runs 归属）")
        nodes.record_run(db, project_id=project_id, task_id=None, node="book_outline",
                         role="Planner", resp=resp, error=resp.error,
                         detail={"genre": genre, "chapter_count": chapter_count})
    if resp.error:
        return {}, resp.error
    try:
        data = nodes._parse_json(resp.content)
    except Exception as exc:  # noqa: BLE001 —— 解析失败同 LLM 失败处理（§6.12）
        return {}, f"parse_error: {exc}"
    if not isinstance(data, dict):
        return {}, "unexpected_json"
    if not isinstance(data.get("volumes"), list):
        return {}, "unexpected_shape: volumes 缺失或非数组"
    return normalize_outline(data) or data, None
