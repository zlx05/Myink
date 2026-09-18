"""Storage helpers for project-scoped model connections.

Connections share the existing JSON settings column under a reserved key, so older
databases need no schema migration. Public API responses always remove encrypted keys.
"""

from __future__ import annotations

from typing import Any

from myink.providers.base import CONFIGURABLE_ROLES

CONNECTIONS_KEY = "__model_connections__"
CUSTOM_ROUTE_PREFIX = "custom:"


def unpack_model_settings(value: dict | None) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    raw = value if isinstance(value, dict) else {}
    routes = {
        role: model_id for role, model_id in raw.items()
        if role in CONFIGURABLE_ROLES and isinstance(model_id, str)
    }
    stored = raw.get(CONNECTIONS_KEY, {})
    connections = {
        str(cid): item for cid, item in stored.items()
        if isinstance(cid, str) and isinstance(item, dict)
    } if isinstance(stored, dict) else {}
    return routes, connections


def keep_custom_routes(routes: dict[str, str], connections: dict[str, dict[str, Any]]) -> dict[str, str]:
    """丢掉已废弃的内置模型 id，只保留指向现存连接的 custom: 路由。"""
    kept: dict[str, str] = {}
    for role, model_ref in routes.items():
        if model_ref.startswith(CUSTOM_ROUTE_PREFIX) and model_ref[len(CUSTOM_ROUTE_PREFIX):] in connections:
            kept[role] = model_ref
    return kept


def pack_model_settings(routes: dict[str, str], connections: dict[str, dict[str, Any]]) -> dict:
    packed: dict[str, Any] = dict(routes)
    if connections:
        packed[CONNECTIONS_KEY] = connections
    return packed


def price_table(item: dict[str, Any] | None) -> dict[str, float] | None:
    """连接上的自定义单价（¥/百万 token）。输入/输出须成对，否则当未配置。"""
    if not isinstance(item, dict):
        return None
    inp, out = item.get("input_price"), item.get("output_price")
    if inp is None or out is None:
        return None
    try:
        input_price = float(inp)
        output_price = float(out)
    except (TypeError, ValueError):
        return None
    hit = item.get("input_cache_hit_price")
    try:
        cache_hit = float(hit) if hit is not None else input_price
    except (TypeError, ValueError):
        cache_hit = input_price
    return {"input": input_price, "input_cache_hit": cache_hit, "output": output_price}


def price_tables_from_packed(packed) -> dict[str, dict[str, float]]:
    """model_id → 单价表；同一 model 多条连接时后者覆盖。"""
    _, connections = unpack_model_settings(packed if isinstance(packed, dict) else {})
    tables: dict[str, dict[str, float]] = {}
    for item in connections.values():
        table = price_table(item)
        model = str(item.get("model", "")).strip()
        if table is not None and model:
            tables[model] = table
    return tables


def public_connections(connections: dict[str, dict[str, Any]]) -> list[dict]:
    return [
        {
            "id": cid,
            "name": str(item.get("name", "")),
            "protocol": str(item.get("protocol", "")),
            "base_url": str(item.get("base_url", "")),
            "model": str(item.get("model", "")),
            "input_price": item.get("input_price"),
            "output_price": item.get("output_price"),
            "has_api_key": bool(item.get("api_key_encrypted")),
        }
        for cid, item in connections.items()
    ]
