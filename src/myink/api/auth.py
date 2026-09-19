"""Local account authentication plus the private-gateway identity boundary.

Authentication endpoints always validate their Bearer token themselves. Existing
business routes continue to trust ``X-Myink-User`` because Python is private and the
gateway overwrites that header after validating and introspecting the token.
"""

from __future__ import annotations

import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from myink.api.schemas import AuthResponse, AuthSessionOut, OkOut
from myink.config import settings
from myink.db import new_session
from myink.invitations import (
    InvitationRejected,
    consume_invitation,
    validate_invitation,
)
from myink.models import Project, User
from myink.passwords import hash_password, validate_password, verify_password

router = APIRouter(prefix="/internal/v1", tags=["auth"])

_ALG = "HS256"
_ISS = "myink"
_USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,64}$", re.ASCII)
_bearer = HTTPBearer(auto_error=False)
_password_slots = threading.BoundedSemaphore(2)
_DUMMY_PASSWORD_HASH = (
    "scrypt$131072$8$1$QOHzV10bCrbhDBCpJPmR2w==$"
    "a_WQOqXzp3DVUmCttce2HN7PyXB-JU1i5MnWF_6dgrzYDt8pY_TK0Jr1d1kYlv6VQOT8zsIMCSEmFEfvrb46jw=="
)


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="INVALID_CREDENTIALS",
        headers={"Cache-Control": "no-store"},
    )


def require_auth_configuration() -> None:
    """Fail closed unless HS256 has at least a 256-bit configured secret."""
    if len(settings.jwt_secret.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=503,
            detail="AUTH_SECRET_NOT_CONFIGURED",
            headers={"Cache-Control": "no-store"},
        )


def normalize_username(username: str) -> str:
    """Trim and ASCII-lower a username, then enforce its canonical alphabet."""
    trimmed = username.strip()
    if not trimmed.isascii():
        raise ValueError("用户名必须是 ASCII")
    canonical = trimmed.lower()
    if not _USERNAME_RE.fullmatch(canonical):
        raise ValueError("用户名必须为 3–64 位 ASCII 字母、数字、下划线或短横线")
    return canonical


@contextmanager
def _password_capacity():
    """Cap concurrent ~128 MiB scrypt operations and fail fast when saturated."""
    if not _password_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="AUTH_CAPACITY_EXCEEDED",
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
        )
    try:
        yield
    finally:
        _password_slots.release()


def create_access_token(
    user_id: uuid.UUID,
    tier: str = "normal",
    auth_version: int = 1,
) -> str:
    """Create a strict, short-lived account token."""
    require_auth_configuration()
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iss": _ISS,
            "tier": tier,
            "ver": auth_version,
            "iat": now,
            "exp": now + timedelta(seconds=settings.jwt_ttl),
        },
        settings.jwt_secret,
        algorithm=_ALG,
    )


def current_user(x_myink_user: str | None = Header(None, alias="X-Myink-User")) -> str | None:
    """Read the identity already authenticated by the private gateway."""
    return x_myink_user


def require_user(user_id: str | None = Depends(current_user)) -> str:
    if not user_id:
        raise HTTPException(status_code=403, detail="缺失身份（未携带已认证用户）")
    try:
        uuid.UUID(user_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=403, detail="身份非法")
    return user_id


def require_owner(project_id: str, user_id: str | None = Depends(current_user)) -> None:
    if not user_id:
        raise HTTPException(status_code=403, detail="缺失身份（未携带已认证用户）")
    try:
        owner = uuid.UUID(user_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=403, detail="身份非法")
    try:
        project_uuid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"项目 id 非法: {project_id}")
    with new_session() as db:
        project = db.get(Project, project_uuid)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        if project.user_id != owner:
            raise HTTPException(status_code=403, detail="无权访问该项目")


class _RegisterRequest(BaseModel):
    username: str
    password: str
    invitation_code: str | None = None

    @field_validator("username")
    @classmethod
    def _valid_username(cls, value: str) -> str:
        return normalize_username(value)

    @field_validator("password")
    @classmethod
    def _valid_password(cls, value: str) -> str:
        return validate_password(value)


class _TokenRequest(BaseModel):
    username: str
    password: str


class _PasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _valid_new_password(cls, value: str) -> str:
        return validate_password(value)


@dataclass(frozen=True)
class _TokenClaims:
    user_id: uuid.UUID
    auth_version: int


@dataclass(frozen=True)
class _AuthenticatedUser:
    id: uuid.UUID
    username: str
    tier: str
    role: str
    password_hash: str | None
    auth_version: int


def _decode_bearer(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> _TokenClaims:
    require_auth_configuration()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _invalid_credentials()
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[_ALG],
            issuer=_ISS,
            options={"require": ["sub", "iss", "iat", "exp", "ver"]},
        )
        user_id = uuid.UUID(claims["sub"])
        auth_version = claims["ver"]
        if isinstance(auth_version, bool) or not isinstance(auth_version, int) or auth_version < 1:
            raise ValueError("invalid token version")
    except (jwt.PyJWTError, KeyError, TypeError, ValueError, AttributeError):
        raise _invalid_credentials()
    return _TokenClaims(user_id=user_id, auth_version=auth_version)


def _authenticated_user(claims: _TokenClaims = Depends(_decode_bearer)) -> _AuthenticatedUser:
    with new_session() as db:
        user = db.get(User, claims.user_id)
        if user is None or user.auth_version != claims.auth_version:
            raise _invalid_credentials()
        return _AuthenticatedUser(
            id=user.id,
            username=user.username,
            tier=user.tier,
            role=user.role,
            password_hash=user.password_hash,
            auth_version=user.auth_version,
        )


def require_admin(
    user: _AuthenticatedUser = Depends(_authenticated_user),
) -> _AuthenticatedUser:
    """Require a strictly authenticated account whose current DB role is admin."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="ADMIN_REQUIRED")
    return user


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _auth_response(user: User) -> dict:
    return {
        "token": create_access_token(user.id, user.tier, user.auth_version),
        "user_id": str(user.id),
        "username": user.username,
        "tier": user.tier,
        "role": user.role,
        "expires_in": settings.jwt_ttl,
    }


@router.post("/auth/register", status_code=status.HTTP_201_CREATED, response_model=AuthResponse)
def register(body: _RegisterRequest, response: Response) -> dict:
    require_auth_configuration()
    try:
        with new_session() as db:
            validate_invitation(db, body.invitation_code)
    except InvitationRejected as exc:
        raise HTTPException(status_code=403, detail=exc.code)
    with _password_capacity():
        password_hash = hash_password(body.password)
    with new_session() as db:
        user = User(
            username=body.username,
            password_hash=password_hash,
            auth_version=1,
            tier="normal",
            role="user",
            environment={},
        )
        try:
            consume_invitation(db, body.invitation_code)
            db.add(user)
            db.flush()
            payload = _auth_response(user)
            db.commit()
        except InvitationRejected as exc:
            db.rollback()
            raise HTTPException(status_code=403, detail=exc.code)
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="USERNAME_TAKEN")
        except Exception:
            db.rollback()
            raise
    _no_store(response)
    return payload


@router.post("/auth/token", response_model=AuthResponse)
def issue_token(body: _TokenRequest, response: Response) -> dict:
    require_auth_configuration()
    if not 1 <= len(body.password) <= 128:
        raise _invalid_credentials()
    try:
        username = normalize_username(body.username)
    except (TypeError, ValueError, AttributeError):
        raise _invalid_credentials()
    with new_session() as db:
        user = db.execute(
            select(User).where(func.lower(func.btrim(User.username)) == username)
        ).scalar_one_or_none()
        stored_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        with _password_capacity():
            password_matches = verify_password(body.password, stored_hash)
        if user is None or not password_matches:
            raise _invalid_credentials()
        payload = _auth_response(user)
    _no_store(response)
    return payload


@router.get("/auth/session", response_model=AuthSessionOut)
def auth_session(response: Response, user: _AuthenticatedUser = Depends(_authenticated_user)) -> dict:
    _no_store(response)
    return {
        "user_id": str(user.id),
        "username": user.username,
        "tier": user.tier,
        "role": user.role,
    }


@router.post("/auth/password", response_model=OkOut)
def change_password(
    body: _PasswordRequest,
    response: Response,
    user: _AuthenticatedUser = Depends(_authenticated_user),
) -> dict:
    with _password_capacity():
        if not verify_password(body.current_password, user.password_hash):
            raise _invalid_credentials()
        new_hash = hash_password(body.new_password)
    with new_session() as db:
        result = db.execute(
            update(User)
            .where(
                User.id == user.id,
                User.auth_version == user.auth_version,
                User.password_hash == user.password_hash,
            )
            .values(password_hash=new_hash, auth_version=User.auth_version + 1)
        )
        if result.rowcount != 1:
            db.rollback()
            raise _invalid_credentials()
        db.commit()
    _no_store(response)
    return {"ok": True}


@router.post("/auth/logout", response_model=OkOut)
def logout(
    response: Response,
    user: _AuthenticatedUser = Depends(_authenticated_user),
) -> dict:
    with new_session() as db:
        result = db.execute(
            update(User)
            .where(User.id == user.id, User.auth_version == user.auth_version)
            .values(auth_version=User.auth_version + 1)
        )
        if result.rowcount != 1:
            db.rollback()
            raise _invalid_credentials()
        db.commit()
    _no_store(response)
    return {"ok": True}
