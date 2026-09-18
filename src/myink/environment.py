"""账号级环境配置：模型连接/路由 + MCP 扫榜（存在 users.environment JSON）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from myink.config import settings
from myink.db import new_session
from myink.models import User
from myink.providers.connections import keep_custom_routes, unpack_model_settings


def default_rankings() -> dict[str, Any]:
    return {
        "enabled": bool(settings.rankings_enabled),
        "mcp_url": settings.rankings_mcp_url,
        "timeout": int(settings.rankings_timeout),
        "limit": int(settings.rankings_limit),
        "source": settings.rankings_source,
        "tool": settings.rankings_tool,
    }


def merge_rankings(raw: Any) -> dict[str, Any]:
    """用户覆盖与进程默认合并；缺字段回落 .env / 内置默认（仅作表单初值）。"""
    base = default_rankings()
    if not isinstance(raw, dict):
        return base
    if "enabled" in raw:
        base["enabled"] = bool(raw["enabled"])
    mcp_url = raw.get("mcp_url")
    if isinstance(mcp_url, str) and mcp_url.strip():
        base["mcp_url"] = mcp_url.strip()
    timeout = raw.get("timeout")
    if isinstance(timeout, int) and 1 <= timeout <= 60:
        base["timeout"] = timeout
    limit = raw.get("limit")
    if isinstance(limit, int) and 1 <= limit <= 50:
        base["limit"] = limit
    source = raw.get("source")
    if isinstance(source, str):
        base["source"] = source.strip()[:32]
    tool = raw.get("tool")
    if isinstance(tool, str):
        base["tool"] = tool.strip()[:80]
    return base


def load_raw(user_id: str | uuid.UUID) -> dict[str, Any]:
    with new_session() as db:
        user = db.get(User, uuid.UUID(str(user_id)))
        raw = user.environment if user is not None else None
        return dict(raw) if isinstance(raw, dict) else {}


def packed_models(user_id: str | uuid.UUID) -> dict[str, Any] | None:
    raw = load_raw(user_id)
    models = raw.get("models")
    return models if isinstance(models, dict) else None


def load_environment(user_id: str | uuid.UUID) -> dict[str, Any]:
    from myink.providers.connections import public_connections

    raw = load_raw(user_id)
    routes, connections = unpack_model_settings(raw.get("models") if isinstance(raw.get("models"), dict) else {})
    routes = keep_custom_routes(routes, connections)
    return {
        "model_routes": routes,
        "model_connections": public_connections(connections),
        "rankings": merge_rankings(raw.get("rankings")),
        "thinking_enabled": bool(raw.get("thinking_enabled")),
    }


def thinking_enabled_for_user(user_id: str | uuid.UUID) -> bool:
    return bool(load_raw(user_id).get("thinking_enabled"))


def save_raw(user_id: str | uuid.UUID, env: dict[str, Any]) -> dict[str, Any]:
    uid = uuid.UUID(str(user_id))
    with new_session() as db:
        user = db.get(User, uid)
        if user is None:
            raise LookupError(f"用户不存在: {uid}")
        user.environment = env
        db.commit()
    return load_environment(uid)


@dataclass
class RankingsSettingsView:
    """供 RankingsService 消费的设置视图（字段名对齐 config.settings）。"""

    rankings_enabled: bool
    rankings_mcp_url: str
    rankings_timeout: int
    rankings_limit: int
    rankings_cache_ttl: int
    rankings_source: str
    rankings_tool: str


def rankings_view_for_user(user_id: str | uuid.UUID) -> RankingsSettingsView | None:
    """用户已保存扫榜覆盖 → 视图；从未保存 → None（调用方走进程默认）。"""
    raw = load_raw(user_id)
    if "rankings" not in raw:
        return None
    merged = merge_rankings(raw.get("rankings"))
    return RankingsSettingsView(
        rankings_enabled=merged["enabled"],
        rankings_mcp_url=merged["mcp_url"],
        rankings_timeout=merged["timeout"],
        rankings_limit=merged["limit"],
        rankings_cache_ttl=int(settings.rankings_cache_ttl),
        rankings_source=merged["source"],
        rankings_tool=merged["tool"],
    )
