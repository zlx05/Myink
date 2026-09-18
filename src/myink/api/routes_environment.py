"""账号级环境配置：模型连接/路由 + MCP 扫榜（不绑具体作品）。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from myink.api.auth import require_user
from myink.api.routes_settings import (
    ConnectionProbeBody,
    ModelConnectionBody,
    _assert_probe_host_allowed,
    _clean_base_url,
    connection_record,
)
from myink.api.schemas import ConnectionTestOut, EnvironmentOut, ModelListOut, RankingsProbeOut
from myink.config import settings
from myink.environment import load_environment, load_raw, save_raw
from myink.integrations.mcp import McpClient, McpError
from myink.providers import CONFIGURABLE_ROLES, probe
from myink.providers.connections import (
    CUSTOM_ROUTE_PREFIX,
    keep_custom_routes,
    pack_model_settings,
    unpack_model_settings,
)
from myink.providers.credentials import decrypt_api_key

router = APIRouter(prefix="/internal/v1", tags=["environment"])


class RankingsBody(BaseModel):
    enabled: bool | None = None
    mcp_url: str | None = None
    timeout: int | None = None
    limit: int | None = None
    source: str | None = None
    tool: str | None = None


class EnvironmentBody(BaseModel):
    """省略字段 = 保留现值；显式传（含 {} / []）= 整体替换。"""

    model_routes: dict[str, str] | None = None
    model_connections: list[ModelConnectionBody] | None = None
    rankings: RankingsBody | None = None
    thinking_enabled: bool | None = None


class RankingsProbeBody(BaseModel):
    mcp_url: str
    timeout: int | None = None


def _validate_rankings(body: RankingsBody, existing: dict) -> dict:
    from myink.environment import merge_rankings

    merged = merge_rankings(existing)
    if body.enabled is not None:
        merged["enabled"] = body.enabled
    if body.mcp_url is not None:
        merged["mcp_url"] = _clean_base_url(body.mcp_url)
    if body.timeout is not None:
        if not 1 <= body.timeout <= 60:
            raise HTTPException(status_code=400, detail="扫榜超时须为 1–60 秒")
        merged["timeout"] = body.timeout
    if body.limit is not None:
        if not 1 <= body.limit <= 50:
            raise HTTPException(status_code=400, detail="扫榜条数须为 1–50")
        merged["limit"] = body.limit
    if body.source is not None:
        source = body.source.strip()
        if len(source) > 32:
            raise HTTPException(status_code=400, detail="榜单来源长度必须 ≤32")
        merged["source"] = source
    if body.tool is not None:
        tool = body.tool.strip()
        if len(tool) > 80:
            raise HTTPException(status_code=400, detail="工具名长度必须 ≤80")
        merged["tool"] = tool
    return merged


def _apply_connections(
    items: list[ModelConnectionBody], existing_connections: dict,
) -> dict:
    connections: dict = {}
    seen: set[str] = set()
    for item in items:
        cid, rec = connection_record(item, existing_connections)
        if cid in seen:
            raise HTTPException(status_code=400, detail=f"模型连接 id 重复: {cid}")
        seen.add(cid)
        connections[cid] = rec
    return connections


def _validate_routes(routes: dict[str, str], connections: dict) -> None:
    for role, model_ref in routes.items():
        if role not in CONFIGURABLE_ROLES:
            raise HTTPException(status_code=400,
                                detail=f"不可配置的 role: {role}（仅 {sorted(CONFIGURABLE_ROLES)}）")
        if model_ref.startswith(CUSTOM_ROUTE_PREFIX) and model_ref[len(CUSTOM_ROUTE_PREFIX):] in connections:
            continue
        raise HTTPException(status_code=400, detail=f"未知模型: {model_ref}")


@router.get("/environment", response_model=EnvironmentOut)
def get_environment(user_id: str = Depends(require_user)) -> dict:
    return load_environment(user_id)


@router.put("/environment", response_model=EnvironmentOut)
def put_environment(body: EnvironmentBody, user_id: str = Depends(require_user)) -> dict:
    raw = load_raw(user_id)
    existing_routes, existing_connections = unpack_model_settings(
        raw.get("models") if isinstance(raw.get("models"), dict) else {},
    )
    routes = existing_routes if body.model_routes is None else body.model_routes
    connections = existing_connections
    if body.model_connections is not None:
        connections = _apply_connections(body.model_connections, existing_connections)
    if body.model_routes is None:
        routes = keep_custom_routes(routes, connections)
    _validate_routes(routes, connections)

    stored = dict(raw)
    stored["models"] = pack_model_settings(routes, connections)
    if body.rankings is not None:
        existing = raw.get("rankings") if isinstance(raw.get("rankings"), dict) else {}
        stored["rankings"] = _validate_rankings(body.rankings, existing)
    if body.thinking_enabled is not None:
        stored["thinking_enabled"] = bool(body.thinking_enabled)
    try:
        return save_raw(user_id, stored)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _probe_key(user_id: str, body: ConnectionProbeBody) -> str:
    raw = body.api_key.get_secret_value().strip() if body.api_key else ""
    if raw:
        return raw
    if body.connection_id:
        _, connections = unpack_model_settings(load_raw(user_id).get("models"))
        stored = connections.get(body.connection_id, {}).get("api_key_encrypted")
        if stored:
            key = decrypt_api_key(str(stored))
            if key:
                return key
    raise HTTPException(status_code=400, detail="需要填写 API Key（或选择已保存密钥的连接）")


@router.post("/environment/models", response_model=ModelListOut)
def list_environment_models(
    body: ConnectionProbeBody, user_id: str = Depends(require_user),
) -> dict:
    base_url = _clean_base_url(body.base_url)
    _assert_probe_host_allowed(base_url)
    api_key = _probe_key(user_id, body)
    models, error = probe.list_models(body.protocol, base_url, api_key)
    return {"ok": error is None, "models": models, "error": error}


@router.post("/environment/test-connection", response_model=ConnectionTestOut)
def test_environment_connection(
    body: ConnectionProbeBody, user_id: str = Depends(require_user),
) -> dict:
    model = (body.model or "").strip()
    if not model or len(model) > 160:
        raise HTTPException(status_code=400, detail="模型 id 长度必须为 1–160")
    base_url = _clean_base_url(body.base_url)
    _assert_probe_host_allowed(base_url)
    api_key = _probe_key(user_id, body)
    ok, latency_ms, reply, error = probe.test_connection(body.protocol, base_url, api_key, model)
    return {"ok": ok, "latency_ms": latency_ms, "reply": reply, "error": error}


@router.post("/environment/test-rankings", response_model=RankingsProbeOut)
async def test_rankings_connection(
    body: RankingsProbeBody, user_id: str = Depends(require_user),
) -> dict:
    del user_id  # 身份已由 require_user 断言；探针不读已存密钥
    base_url = _clean_base_url(body.mcp_url)
    _assert_probe_host_allowed(base_url)
    timeout = settings.rankings_timeout if body.timeout is None else body.timeout
    if not 1 <= timeout <= 60:
        raise HTTPException(status_code=400, detail="扫榜超时须为 1–60 秒")
    try:
        async with McpClient(base_url, timeout_s=timeout) as client:
            tools = await client.list_tools()
        return {"ok": True, "tools": tools[:50], "error": None}
    except asyncio.CancelledError:
        return {"ok": False, "tools": [], "error": "扫榜请求被取消"}
    except McpError as exc:
        return {"ok": False, "tools": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 外部协议不可信，探针只回报失败
        return {"ok": False, "tools": [], "error": f"扫榜连接失败: {exc}"}
