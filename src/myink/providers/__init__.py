"""Model providers and project-scoped routing with a default fallback chain."""

from __future__ import annotations

import uuid
from typing import Any

from myink.providers.anthropic import AnthropicProvider
from myink.providers.base import (
    CONFIGURABLE_ROLES,
    DEFAULT_ROUTES,
    MODEL_REGISTRY,
    FallbackChain,
    MissingModelProvider,
    ModelProvider,
    ModelResponse,
    ModelSpec,
    effective_cost,
    estimate_cost,
    lookup_prices,
)
from myink.providers.connections import CUSTOM_ROUTE_PREFIX, price_table, unpack_model_settings
from myink.providers.credentials import decrypt_api_key
from myink.providers.deepseek import DeepSeekProvider
from myink.providers.openai_compatible import OpenAICompatibleProvider
from myink.providers.prices import DEEPSEEK_PRICES

# 测试可替换此单例以注入 stub。生产路径不再用它打 .env DeepSeek。
_BUILTIN_PROVIDER = DeepSeekProvider()
default_provider = _BUILTIN_PROVIDER
_UNCONFIGURED = MissingModelProvider()

# 审核/摘要未单独配置时，沿用用户已填的可配置角色。
_ROLE_INHERIT: dict[str, tuple[str, ...]] = {
    "audit": ("validator_l2", "writer", "planner", "extract"),
    "summarize": ("extract", "writer", "planner"),
}


def _no_model_chain(thinking: bool) -> FallbackChain:
    if default_provider is not _BUILTIN_PROVIDER:
        return FallbackChain(default_provider, ["stub"], thinking_enabled=thinking)
    return FallbackChain(_UNCONFIGURED, ["unconfigured"], thinking_enabled=thinking)


def make_chain(role: str, project_id=None, db=None) -> FallbackChain:
    """只走用户环境/作品里的自备连接；未配置则明确报错，不再回落内置 DeepSeek。"""
    thinking = _thinking_enabled(project_id, db) if project_id is not None else False
    # 测试替换了 default_provider：整条链走 stub，避免本机账号环境打到真实模型。
    if default_provider is not _BUILTIN_PROVIDER:
        return FallbackChain(default_provider, ["stub"], thinking_enabled=thinking)
    override = _project_primary(role, project_id, db) if project_id is not None else None
    if not isinstance(override, dict):
        return _no_model_chain(thinking)

    api_key = decrypt_api_key(str(override.get("api_key_encrypted", "")))
    if not api_key:
        return _no_model_chain(thinking)
    protocol = override.get("protocol")
    if protocol == "openai":
        provider: ModelProvider = OpenAICompatibleProvider(
            api_key=api_key, base_url=str(override.get("base_url", "")))
    elif protocol == "anthropic":
        provider = AnthropicProvider(api_key=api_key, base_url=str(override.get("base_url", "")))
    else:
        return _no_model_chain(thinking)
    model = str(override.get("model", "")).strip()
    if not model:
        return _no_model_chain(thinking)
    return FallbackChain(
        provider, [model], providers=[provider], thinking_enabled=thinking,
        prices=price_table(override),
    )


def _from_packed(role: str, packed) -> dict[str, Any] | None:
    routes, connections = unpack_model_settings(packed)
    route = routes.get(role)
    if route and route.startswith(CUSTOM_ROUTE_PREFIX):
        return connections.get(route[len(CUSTOM_ROUTE_PREFIX):])
    return None


def _lookup_override(role: str, packed) -> dict[str, Any] | None:
    hit = _from_packed(role, packed)
    if hit is not None:
        return hit
    for alias in _ROLE_INHERIT.get(role, ()):
        hit = _from_packed(alias, packed)
        if hit is not None:
            return hit
    return None


def _thinking_enabled(project_id, db) -> bool:
    from myink.environment import thinking_enabled_for_user
    from myink.models import Project

    pid = uuid.UUID(str(project_id))
    if db is None:
        from myink.db import tenant_session
        with tenant_session(str(pid)) as session:
            proj = session.get(Project, pid)
            owner = proj.user_id if proj is not None else None
    else:
        proj = db.get(Project, pid)
        owner = proj.user_id if proj is not None else None
    return thinking_enabled_for_user(owner) if owner is not None else False


def _project_primary(role: str, project_id, db) -> dict[str, Any] | None:
    if role not in CONFIGURABLE_ROLES and role not in _ROLE_INHERIT:
        return None
    from myink.db import tenant_session
    from myink.environment import packed_models
    from myink.memory.repository import get_settings
    from myink.models import Project

    pid = uuid.UUID(str(project_id))
    if db is None:
        with tenant_session(str(pid)) as session:
            proj = session.get(Project, pid)
            owner = proj.user_id if proj is not None else None
            st = get_settings(session, pid)
    else:
        proj = db.get(Project, pid)
        owner = proj.user_id if proj is not None else None
        st = get_settings(db, pid)
    if owner is not None:
        hit = _lookup_override(role, packed_models(owner))
        if hit is not None:
            return hit
    if st is None:
        return None
    return _lookup_override(role, st.model_routes)


__all__ = [
    "AnthropicProvider",
    "CONFIGURABLE_ROLES",
    "DEFAULT_ROUTES",
    "MODEL_REGISTRY",
    "DEEPSEEK_PRICES",
    "FallbackChain",
    "MissingModelProvider",
    "ModelProvider",
    "ModelResponse",
    "ModelSpec",
    "DeepSeekProvider",
    "OpenAICompatibleProvider",
    "default_provider",
    "effective_cost",
    "estimate_cost",
    "lookup_prices",
    "make_chain",
]
