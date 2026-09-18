"""Encrypt project-scoped model credentials before storing them in settings JSON."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from myink.config import settings


def _cipher() -> Fernet:
    secret = settings.model_credential_key or settings.jwt_secret
    digest = hashlib.sha256(f"myink:model-credentials:{secret}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str | None:
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        return None
