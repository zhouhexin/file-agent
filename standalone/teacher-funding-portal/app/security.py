from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_text, digest_text = stored.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = _unb64(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt_text), 310_000)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def issue_token(username: str, role: str, secret: str, ttl_hours: int, token_version: int) -> str:
    payload = _b64(json.dumps({"sub": username, "role": role,
                               "ver": token_version,
                               "exp": int(time.time()) + ttl_hours * 3600},
                              separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return payload + "." + signature


def read_token(token: str, secret: str) -> dict:
    try:
        if len(token) > 2048:
            raise ValueError("oversized token")
        payload_part, signature = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), payload_part.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        payload = json.loads(_unb64(payload_part))
        if not isinstance(payload, dict):
            raise ValueError("bad claims")
        if type(payload.get("exp")) is not int or payload["exp"] <= int(time.time()):
            raise ValueError("expired")
        if not payload.get("sub") or payload.get("role") not in {"user", "admin"}:
            raise ValueError("bad claims")
        if not isinstance(payload["sub"], str) or type(payload.get("ver")) is not int:
            raise ValueError("bad claims")
        return payload
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid token") from exc


def configured_secret() -> str:
    value = os.getenv("PORTAL_JWT_SECRET", "")
    if len(value) < 32:
        raise RuntimeError("PORTAL_JWT_SECRET must contain at least 32 characters")
    return value
