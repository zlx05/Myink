"""文风样本提取内部端点（§7.12 文风档案闭环：统计层 + LLM 提炼 → 草稿 → 确认落库）。

- POST /projects/{pid}/style-samples：作者样本 → 统计层（确定性）+ LLM 提炼（extract 档
  一次调用）→ 合并返回 StyleProfile **草稿**（不落库）；LLM 失败降级只回统计层 +
  extract_error（§6.12 不 500、不阻塞）；extract 调用记 agent_runs（§6.8 成本透明）。
- PUT /projects/{pid}/style-profile：前端可编辑草稿后回传 → 校验 → 编排层落库
  project_settings.style_profile + 递增 version（乐观版本号 §7.6）；键级保留 L1 基线键
  fatigue_words/patterns（样本草稿确认不抹预设，显式 [] 可清空）。

与 §7.11 设定治理权威模型一致：agent 只提案、用户确认是唯一 canon（不走记忆候选池）；
数据流边界（§6.2）：确认 = 编排层写库入口，与 persist 同层。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from myink.api.auth import require_owner
from myink.api.schemas import SkillPresetOut, StyleDraftOut, StyleProfileOut
from myink.db import new_session, tenant_session
from myink.memory.repository import get_settings
from myink.models import ProjectSettings
from myink.seed import STYLE_PRESETS
from myink.style_extract import (
    analyze_sample_stats,
    extract_style_profile,
    merge_style_draft,
    validate_profile,
)

router = APIRouter(prefix="/internal/v1", tags=["style"])

# 样本上限（1–2 篇、总量 1.2 万字）——防 prompt 溢出（extract 档上下文窗口内，§6.12）
_MAX_SAMPLES = 2
_MAX_TOTAL_CHARS = 12_000


def _pid(project_id: str) -> uuid.UUID:
    """path 里的 project_id 转 uuid（require_owner 已校验格式合法，此处仅类型转换）。"""
    return uuid.UUID(project_id)


class StyleSamplesBody(BaseModel):
    """作者样本（1–2 篇）。"""

    samples: list[str]


@router.get("/skill-presets", response_model=list[SkillPresetOut])
def skill_presets() -> list[dict]:
    """题材 Skill 预设列表（§7.12 预设包）：4 本种子书文风档案，设置页「预设导入」渲染。

    静态数据（无租户隔离），网关 JWT 已认证；预设 id 同时是 skill_pack marker。
    """
    return [{"id": p["id"], "name": p["name"], "genre": p["genre"],
             "style_profile": p["style_profile"]} for p in STYLE_PRESETS]


@router.post("/projects/{project_id}/style-samples",
             dependencies=[Depends(require_owner)], response_model=StyleDraftOut)
def style_samples(project_id: str, body: StyleSamplesBody) -> dict:
    """样本 → 统计层 + LLM 提炼 → 文风档案草稿（不落库）。

    extract 调用记 agent_runs（§6.8 成本透明）；agent_runs 无 RLS（观测表），普通连接可写。
    """
    samples = [s.strip() for s in body.samples if s.strip()]
    if not samples:
        raise HTTPException(status_code=400, detail="至少提供一篇非空样本")
    if len(samples) > _MAX_SAMPLES:
        raise HTTPException(status_code=400, detail=f"样本最多 {_MAX_SAMPLES} 篇")
    if sum(len(s) for s in samples) > _MAX_TOTAL_CHARS:
        raise HTTPException(status_code=400, detail=f"样本总量不超过 {_MAX_TOTAL_CHARS} 字")

    stats = analyze_sample_stats(samples)
    db = new_session()
    try:
        llm_profile, extract_error = extract_style_profile(samples, stats,
                                                           project_id=project_id, db=db)
        db.commit()
    finally:
        db.close()
    draft = merge_style_draft(stats, llm_profile, extract_error=extract_error)
    return {"draft": draft}


class StyleProfileBody(BaseModel):
    """文风档案（前端可编辑草稿后回传；dict 透传，落库前轻校验）。

    skill_pack：题材预设 marker（§7.12 预设导入原子写，值为预设 id）；None 保留现值。
    """

    profile: dict
    skill_pack: str | None = None


@router.put("/projects/{project_id}/style-profile",
            dependencies=[Depends(require_owner)], response_model=StyleProfileOut)
def put_style_profile(project_id: str, body: StyleProfileBody) -> dict:
    """确认落库：编排层写 project_settings.style_profile + version 递增（§7.6 乐观版本号）。

    - 剔除瞬态诊断键 extract_error（草稿降级提示不落库）；
    - 键级保留 L1 基线键 fatigue_words/patterns：样本草稿白名单收键不含检测基线，确认时不抹
      预设（显式传 [] 可清空）；其余键仍整档案覆盖；
    - body.skill_pack 非 None 时一并落 skill_pack（预设导入 = profile + marker 原子写）。
    """
    profile = dict(body.profile)
    profile.pop("extract_error", None)
    try:
        profile = validate_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with tenant_session(project_id) as db:
        st = get_settings(db, _pid(project_id))
        if st is None:
            # 新行：列默认 version=1（本切片起计数）；已存在的行每次确认 +1
            st = ProjectSettings(project_id=_pid(project_id), style_profile=profile, version=1)
            db.add(st)
        else:
            existing = st.style_profile or {}
            for k in ("fatigue_words", "fatigue_patterns"):
                if k not in profile and existing.get(k) is not None:
                    profile[k] = existing[k]
            st.style_profile = profile
            st.version = (st.version or 1) + 1
        if body.skill_pack is not None:
            st.skill_pack = body.skill_pack
        db.commit()
        new_version = st.version
    return {"style_profile": profile, "skill_pack": st.skill_pack, "version": new_version}
