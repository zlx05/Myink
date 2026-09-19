"""Password validation and self-describing scrypt hashes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

SCRYPT_N = 131_072
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 64
_SALT_BYTES = 16
_MAXMEM = 256 * 1024 * 1024
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]", re.ASCII)
_ASCII_DIGIT_RE = re.compile(r"[0-9]", re.ASCII)


def validate_password(password: str) -> str:
    """Return a valid password, measured in Unicode characters."""
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError("密码长度必须为 8–128 个字符")
    if not _ASCII_LETTER_RE.search(password) or not _ASCII_DIGIT_RE.search(password):
        raise ValueError("密码必须至少包含 1 个 ASCII 字母和 1 个 ASCII 数字")
    return password


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_DKLEN,
        maxmem=_MAXMEM,
    )


def hash_password(password: str) -> str:
    """Hash a validated password with a new cryptographic salt."""
    validate_password(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_text}${digest_text}"


def verify_password(password: str, stored: str | None) -> bool:
    """Verify a self-describing hash; malformed/legacy values fail closed."""
    if not stored:
        return False
    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = base64.b64decode(salt_text.encode("ascii"), altchars=b"-_", validate=True)
        expected = base64.b64decode(digest_text.encode("ascii"), altchars=b"-_", validate=True)
        if len(salt) < _SALT_BYTES or len(expected) != _DKLEN:
            return False
        actual = _derive(password, salt, n=n, r=r, p=p)
    except (ArithmeticError, UnicodeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)
