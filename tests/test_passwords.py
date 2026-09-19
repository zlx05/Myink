"""Password storage behavior; expectations are independent of implementation helpers."""

from __future__ import annotations

import base64
import importlib

import pytest


def _passwords():
    try:
        return importlib.import_module("myink.passwords")
    except ModuleNotFoundError:
        pytest.fail("myink.passwords is missing")


def test_scrypt_hash_uses_required_parameters_random_salt_and_constant_verification_contract():
    passwords = _passwords()
    first = passwords.hash_password("correct horse battery 1")
    second = passwords.hash_password("correct horse battery 1")
    assert first != second
    assert "correct horse battery 1" not in first
    scheme, n, r, p, salt, digest = first.split("$")
    assert (scheme, n, r, p) == ("scrypt", "131072", "8", "1")
    assert len(base64.urlsafe_b64decode(salt.encode("ascii"))) >= 16
    assert len(base64.urlsafe_b64decode(digest.encode("ascii"))) >= 32
    assert passwords.verify_password("correct horse battery 1", first) is True
    assert passwords.verify_password("wrong horse battery", first) is False


@pytest.mark.parametrize("stored", ["", "plaintext", "scrypt$bad$8$1$salt$digest"])
def test_malformed_password_hash_is_rejected_without_error(stored: str):
    assert _passwords().verify_password("correct horse battery", stored) is False


@pytest.mark.parametrize(
    "password",
    ["a1short", "x" * 129, "abcdefgh", "12345678", "密码123456"],
)
def test_new_password_validation_enforces_length_and_ascii_letter_digit(password: str):
    with pytest.raises(ValueError):
        _passwords().validate_password(password)


def test_password_validation_accepts_eight_to_128_unicode_characters_and_symbols():
    passwords = _passwords()
    assert passwords.validate_password("a1测试✅!!!") == "a1测试✅!!!"
    assert passwords.validate_password("a1" + "§" * 126) == "a1" + "§" * 126
