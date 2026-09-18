"""身份断言（plan.md §14.1 ③）：JWT 签发 + 归属校验依赖，替换阶段 2 X-Myink-User 占位。

- 签发：`POST /internal/v1/auth/token` —— username → JWT（HS256，sub=user_id，短时效）。
  MVP 无密码（User 表无 password 字段），用户名即身份（开发/演示口径）；生产换
  OAuth/密码 + 签发限流（§14.2 SSO/token 行）。
- `current_user`：读 `X-Myink-User` 头（网关验证 JWT 后透传的可信身份），供应用层过滤。
- `require_owner`：**route-level 依赖**（挂在 `@router(..., dependencies=[Depends(require_owner)])`
  上，不改端点函数签名）——断言 `project.user_id == 请求者`，RLS 之外的应用层第二道门
  （网关验 JWT = 第一道）。project 不存在 → 404；存在但不属于请求者 → 403（fail closed）；
  缺身份 / 身份非法 → 403。

信任链（§17.2）：网关是唯一公网入口，验 JWT → 解出可信 user_id → 透传 X-Myink-User →
Python 读该头断言归属。Python 仅监听 127.0.0.1:8100 不暴露公网，信任该头 = 已验证身份。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from myink.api.schemas import AuthResponse
from myink.config import settings
from myink.db import new_session
from myink.models import Project, User

router = APIRouter(prefix="/internal/v1", tags=["auth"])

_ALG = "HS256"
_ISS = "myink"


def create_access_token(user_id: uuid.UUID, tier: str = "normal") -> str:
    """签 JWT：HS256、sub=user_id、iss=myink、tier（阶段 6 VIP 优先级）、短时效（§14.2 token 短期有效）。

    tier claim 由网关 verifyJWT 读出，VIP → RabbitMQ 消息高优先级（vip→9/normal→0）。
    """
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iss": _ISS,
            "tier": tier,
            "iat": now,
            "exp": now + timedelta(seconds=settings.jwt_ttl),
        },
        settings.jwt_secret,
        algorithm=_ALG,
    )


def current_user(x_myink_user: str | None = Header(None, alias="X-Myink-User")) -> str | None:
    """读网关透传的已验证身份（X-Myink-User = JWT 的 sub）。空/缺失 → None（调用方 fail closed）。"""
    return x_myink_user


def require_user(user_id: str | None = Depends(current_user)) -> str:
    """已认证用户（无项目归属）。缺失/非法身份 → 403。供账号级端点（环境配置、扫榜）挂载。"""
    if not user_id:
        raise HTTPException(status_code=403, detail="缺失身份（未携带已认证用户）")
    try:
        uuid.UUID(user_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=403, detail="身份非法")
    return user_id


def require_owner(project_id: str, user_id: str | None = Depends(current_user)) -> None:
    """归属断言：project 必须属于请求者。失败 404（不存在）/ 403（越权或身份缺失/非法）。

    route-level dependencies 挂载 → 只在 FastAPI HTTP 层执行，直接函数调用（单测）不受影响
    ——安全强制集中在入口，不散在各函数（§14.1 默认拒绝 + 强制集中）。
    """
    if not user_id:
        raise HTTPException(status_code=403, detail="缺失身份（未携带已认证用户）")
    try:
        owner = uuid.UUID(user_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=403, detail="身份非法")
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"项目 id 非法: {project_id}")
    with new_session() as db:
        project = db.get(Project, pid)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        if project.user_id != owner:
            raise HTTPException(status_code=403, detail="无权访问该项目")


class _TokenRequest(BaseModel):
    username: str


@router.post("/auth/token", response_model=AuthResponse)
def issue_token(body: _TokenRequest) -> dict:
    """签发 JWT（MVP 无密码：用户名即身份，与 seed demo 用户对齐）。"""
    if settings.is_prod():
        raise HTTPException(status_code=403, detail="DEMO_LOGIN_DISABLED")
    with new_session() as db:
        user = db.query(User).filter(User.username == body.username).first()
        if user is None:
            raise HTTPException(status_code=404, detail=f"用户不存在: {body.username}")
        return {
            "token": create_access_token(user.id, tier=user.tier),
            "user_id": str(user.id),
            "tier": user.tier,
            "expires_in": settings.jwt_ttl,
        }
