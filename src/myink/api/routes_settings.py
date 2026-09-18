"""Project writing settings and configurable model connections."""

from __future__ import annotations

import ipaddress
import socket
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr

from myink.api.auth import require_owner
from myink.api.schemas import ConnectionTestOut, ModelListOut, ProjectSettingsOut
from myink.db import tenant_session
from myink.memory.repository import get_settings
from myink.models import ProjectSettings
from myink.providers import CONFIGURABLE_ROLES, probe
from myink.providers.connections import (
    CUSTOM_ROUTE_PREFIX,
    keep_custom_routes,
    pack_model_settings,
    public_connections,
    unpack_model_settings,
)
from myink.genre_catalog import public_pack
from myink.providers.credentials import decrypt_api_key, encrypt_api_key

router = APIRouter(prefix="/internal/v1", tags=["settings"])


def _pid(project_id: str) -> uuid.UUID:
    return uuid.UUID(project_id)


class ModelConnectionBody(BaseModel):
    id: str
    name: str
    protocol: Literal["openai", "anthropic"]
    base_url: str
    model: str
    api_key: SecretStr | None = None
    input_price: float | None = None
    output_price: float | None = None


class SettingsBody(BaseModel):
    """两个字段都是「省略 = 保留现值，显式传（含 {} / []）= 整体替换」。

    原先 model_routes 省略会被当成空字典覆盖，只改连接的客户端会静默丢掉全部分角色配置。
    """

    model_routes: dict[str, str] | None = None
    model_connections: list[ModelConnectionBody] | None = None


class ConnectionProbeBody(BaseModel):
    """探针请求：未保存的新连接直接传明文 api_key；已保存连接可只给 connection_id 复用密钥。"""

    protocol: Literal["openai", "anthropic"]
    base_url: str
    model: str | None = None
    api_key: SecretStr | None = None
    connection_id: str | None = None


def _clean_base_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="请求地址必须是有效的 http/https 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="请求地址不能包含账号、密码、查询参数或片段")
    return base_url


def _unsafe_address(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return True  # 解析不出（含 zone id 等）→ 保守拒绝
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved


def _assert_probe_host_allowed(base_url: str) -> None:
    """探针地址守卫：只拦纯滥用目标——链路本地（含 169.254.169.254 云元数据）、多播、未指定、
    保留段。私有段与回环**刻意放行**：本地推理服务（如 127.0.0.1:11434 Ollama）正是本功能的
    主场景，拦住等于砍掉正常用法；生成路径本就能打任意地址，探针不引入新的越权面。

    解析后才判（域名解析到链路本地同样拒绝）；解析失败不在此处改写成另一种错误，交给探针
    自己报「连不上」，让失败原因来自真实请求。
    """
    host = urlsplit(base_url).hostname
    if not host:
        raise HTTPException(status_code=400, detail="请求地址缺少主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return
    if any(_unsafe_address(info[4][0]) for info in infos):
        raise HTTPException(status_code=400, detail="请求地址指向内网元数据/链路本地端点，已拒绝")


def _validate_connection(item: ModelConnectionBody) -> tuple[str, str, str, str]:
    try:
        cid = str(uuid.UUID(item.id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="模型连接 id 必须是 UUID") from exc
    name = item.name.strip()
    model = item.model.strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="模型连接名称长度必须为 1–80")
    if not model or len(model) > 160:
        raise HTTPException(status_code=400, detail="模型 id 长度必须为 1–160")
    return cid, name, _clean_base_url(item.base_url), model


def _price_fields(item: ModelConnectionBody) -> dict:
    if item.input_price is None and item.output_price is None:
        return {}
    if item.input_price is None or item.output_price is None:
        raise HTTPException(status_code=400, detail="输入价与输出价须同时填写")
    for label, value in (("输入价", item.input_price), ("输出价", item.output_price)):
        if value < 0 or value > 10_000:
            raise HTTPException(status_code=400, detail=f"{label}须为 0–10000（¥/百万 token）")
    return {"input_price": float(item.input_price), "output_price": float(item.output_price)}


def connection_record(item: ModelConnectionBody, existing_connections: dict) -> tuple[str, dict]:
    cid, name, base_url, model = _validate_connection(item)
    raw_key = item.api_key.get_secret_value().strip() if item.api_key else ""
    encrypted = encrypt_api_key(raw_key) if raw_key else existing_connections.get(cid, {}).get("api_key_encrypted")
    if not encrypted:
        raise HTTPException(status_code=400, detail=f"新模型连接“{name}”必须填写 API Key")
    return cid, {
        "name": name, "protocol": item.protocol, "base_url": base_url,
        "model": model, "api_key_encrypted": encrypted, **_price_fields(item),
    }


def _response(st: ProjectSettings | None) -> dict:
    if st is None:
        return {
            "style_profile": {}, "skill_pack": None, "genre_pack": {},
            "model_routes": {}, "model_connections": [], "version": 0,
        }
    routes, connections = unpack_model_settings(st.model_routes)
    return {
        "style_profile": st.style_profile or {},
        "skill_pack": st.skill_pack,
        "genre_pack": public_pack(st.genre_pack),
        "model_routes": keep_custom_routes(routes, connections),
        "model_connections": public_connections(connections),
        "version": st.version or 0,
    }


@router.get("/projects/{project_id}/settings",
            dependencies=[Depends(require_owner)], response_model=ProjectSettingsOut)
def get_project_settings(project_id: str) -> dict:
    with tenant_session(project_id) as db:
        return _response(get_settings(db, _pid(project_id)))


@router.put("/projects/{project_id}/settings",
            dependencies=[Depends(require_owner)], response_model=ProjectSettingsOut)
def put_project_settings(project_id: str, body: SettingsBody) -> dict:
    with tenant_session(project_id) as db:
        st = get_settings(db, _pid(project_id))
        existing_routes, existing_connections = unpack_model_settings(st.model_routes if st else None)
        # 省略 model_routes = 保留现值（与 model_connections 对称）：旧写法 `or {}` 分不清
        # 「没传」与「传空」，只改连接的客户端会把全部分角色路由清空。
        routes = existing_routes if body.model_routes is None else body.model_routes
        connections = existing_connections
        if body.model_connections is not None:
            connections = {}
            seen: set[str] = set()
            for item in body.model_connections:
                cid, rec = connection_record(item, existing_connections)
                if cid in seen:
                    raise HTTPException(status_code=400, detail=f"模型连接 id 重复: {cid}")
                seen.add(cid)
                connections[cid] = rec
        if body.model_routes is None:
            routes = keep_custom_routes(routes, connections)

        for role, model_ref in routes.items():
            if role not in CONFIGURABLE_ROLES:
                raise HTTPException(status_code=400,
                                    detail=f"不可配置的 role: {role}（仅 {sorted(CONFIGURABLE_ROLES)}）")
            if model_ref.startswith(CUSTOM_ROUTE_PREFIX) and model_ref[len(CUSTOM_ROUTE_PREFIX):] in connections:
                continue
            raise HTTPException(status_code=400, detail=f"未知模型: {model_ref}")

        stored = pack_model_settings(routes, connections)
        if st is None:
            st = ProjectSettings(project_id=_pid(project_id), model_routes=stored, version=1)
            db.add(st)
        else:
            st.model_routes = stored
            st.version = (st.version or 1) + 1
        result = _response(st)
        db.commit()
        return result


def _probe_key(project_id: str, body: ConnectionProbeBody) -> str:
    """明文 api_key 优先；否则用 connection_id 取已存密文解密。都没有 → 400。"""
    raw = body.api_key.get_secret_value().strip() if body.api_key else ""
    if raw:
        return raw
    if body.connection_id:
        with tenant_session(project_id) as db:
            st = get_settings(db, _pid(project_id))
            _, connections = unpack_model_settings(st.model_routes if st else None)
        stored = connections.get(body.connection_id, {}).get("api_key_encrypted")
        if stored:
            key = decrypt_api_key(str(stored))
            if key:
                return key
    raise HTTPException(status_code=400, detail="需要填写 API Key（或选择已保存密钥的连接）")


@router.post("/projects/{project_id}/settings/models",
             dependencies=[Depends(require_owner)], response_model=ModelListOut)
def list_connection_models(project_id: str, body: ConnectionProbeBody) -> dict:
    base_url = _clean_base_url(body.base_url)
    _assert_probe_host_allowed(base_url)
    api_key = _probe_key(project_id, body)
    models, error = probe.list_models(body.protocol, base_url, api_key)
    return {"ok": error is None, "models": models, "error": error}


@router.post("/projects/{project_id}/settings/test-connection",
             dependencies=[Depends(require_owner)], response_model=ConnectionTestOut)
def test_model_connection(project_id: str, body: ConnectionProbeBody) -> dict:
    model = (body.model or "").strip()
    if not model or len(model) > 160:
        raise HTTPException(status_code=400, detail="模型 id 长度必须为 1–160")
    base_url = _clean_base_url(body.base_url)
    _assert_probe_host_allowed(base_url)
    api_key = _probe_key(project_id, body)
    ok, latency_ms, reply, error = probe.test_connection(body.protocol, base_url, api_key, model)
    return {"ok": ok, "latency_ms": latency_ms, "reply": reply, "error": error}
