"""认证安全工具。

当前实现使用标准库完成 PBKDF2 密码哈希和 HS256 JWT，避免在早期阶段引入额外依赖。
生产环境可以替换为 passlib 和成熟 JWT 库，但外部接口保持不变。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import get_settings


class TokenDecodeError(ValueError):
    """token 解析或签名校验失败时抛出。"""

    pass


PUBLIC_FILE_ACCESS_AUDIENCE = "file-agent-public-file-access-v1"
PUBLIC_CLASSIFICATION_ACCESS_AUDIENCE = "file-agent-public-classification-access-v1"


def hash_password(password: str) -> str:
    """使用 PBKDF2 生成带盐密码哈希。"""

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码和存储哈希是否匹配。"""

    try:
        algorithm, salt, digest = password_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(candidate.hex(), digest)


def create_access_token(user_id: str, role: str) -> str:
    """创建 HS256 JWT access token。"""

    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "role": role,
        "exp": int(expires_at.timestamp()),
    }
    header = {"alg": settings.jwt_algorithm, "typ": "JWT"}
    signing_input = ".".join([
        _b64_json(header),
        _b64_json(payload),
    ])
    signature = _sign(signing_input)
    return f"{signing_input}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    """解析并校验 access token，失败时统一抛出 TokenDecodeError。"""

    try:
        header_part, payload_part, signature = token.split(".", 2)
    except ValueError as exc:
        raise TokenDecodeError("Invalid token format") from exc

    signing_input = f"{header_part}.{payload_part}"
    expected_signature = _sign(signing_input)
    if not hmac.compare_digest(signature, expected_signature):
        raise TokenDecodeError("Invalid token signature")

    payload = _decode_json(payload_part)
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at < int(datetime.now(timezone.utc).timestamp()):
        raise TokenDecodeError("Token expired")
    if not payload.get("sub"):
        raise TokenDecodeError("Missing subject")
    return payload


def create_public_file_access_token(working_copy_id: str) -> str:
    """为活动工作副本创建无到期时间的只读公开访问能力令牌。

    令牌只保存稳定工作副本 ID，不包含用户身份、文件路径或正文。它使用部署实例的
    JWT 密钥签名，但拥有独立 audience，不能被当作登录令牌使用。
    """

    normalized = str(working_copy_id or "").strip()
    if not normalized or len(normalized) > 36 or any(ord(item) < 32 for item in normalized):
        raise ValueError("working_copy_id 格式无效")
    payload_part = _b64_json(
        {
            "aud": PUBLIC_FILE_ACCESS_AUDIENCE,
            "working_copy_id": normalized,
        }
    )
    signature = _sign(f"{PUBLIC_FILE_ACCESS_AUDIENCE}.{payload_part}")
    return f"{payload_part}.{signature}"


def decode_public_file_access_token(token: str) -> dict[str, str]:
    """校验公开文件能力令牌；该令牌按产品要求不检查过期时间。"""

    normalized = str(token or "").strip()
    if not normalized or len(normalized) > 512:
        raise TokenDecodeError("Invalid public file access token")
    try:
        payload_part, signature = normalized.split(".", 1)
    except ValueError as exc:
        raise TokenDecodeError("Invalid public file access token") from exc
    expected_signature = _sign(f"{PUBLIC_FILE_ACCESS_AUDIENCE}.{payload_part}")
    if not hmac.compare_digest(signature, expected_signature):
        raise TokenDecodeError("Invalid public file access token")
    payload = _decode_json(payload_part)
    if not isinstance(payload, dict):
        raise TokenDecodeError("Invalid public file access token")
    working_copy_id = str(payload.get("working_copy_id") or "").strip()
    if payload.get("aud") != PUBLIC_FILE_ACCESS_AUDIENCE or not working_copy_id:
        raise TokenDecodeError("Invalid public file access token")
    return {
        "aud": PUBLIC_FILE_ACCESS_AUDIENCE,
        "working_copy_id": working_copy_id,
    }


def create_public_classification_access_token() -> str:
    """创建无到期时间的只读分类页面能力令牌，不包含登录用户身份。"""

    payload_part = _b64_json(
        {
            "aud": PUBLIC_CLASSIFICATION_ACCESS_AUDIENCE,
            "scope": "organization-classification-read",
        }
    )
    signature = _sign(f"{PUBLIC_CLASSIFICATION_ACCESS_AUDIENCE}.{payload_part}")
    return f"{payload_part}.{signature}"


def decode_public_classification_access_token(token: str) -> dict[str, str]:
    """校验 WorkBuddy 分类页能力令牌；该只读令牌按产品要求不设置过期时间。"""

    normalized = str(token or "").strip()
    if not normalized or len(normalized) > 512:
        raise TokenDecodeError("Invalid public classification access token")
    try:
        payload_part, signature = normalized.split(".", 1)
    except ValueError as exc:
        raise TokenDecodeError("Invalid public classification access token") from exc
    expected_signature = _sign(
        f"{PUBLIC_CLASSIFICATION_ACCESS_AUDIENCE}.{payload_part}"
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise TokenDecodeError("Invalid public classification access token")
    payload = _decode_json(payload_part)
    if (
        payload.get("aud") != PUBLIC_CLASSIFICATION_ACCESS_AUDIENCE
        or payload.get("scope") != "organization-classification-read"
    ):
        raise TokenDecodeError("Invalid public classification access token")
    return {
        "aud": PUBLIC_CLASSIFICATION_ACCESS_AUDIENCE,
        "scope": "organization-classification-read",
    }


def _b64_json(data: dict[str, Any]) -> str:
    """把 JSON 对象编码为 JWT 使用的 base64url 片段。"""

    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _b64_encode(raw)


def _decode_json(data: str) -> dict[str, Any]:
    """把 base64url 片段解码为 JSON 对象。"""

    try:
        return json.loads(_b64_decode(data))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenDecodeError("Invalid token payload") from exc


def _sign(signing_input: str) -> str:
    """使用配置中的密钥生成 HS256 签名。"""

    settings = get_settings()
    if settings.jwt_algorithm != "HS256":
        raise TokenDecodeError("Only HS256 is supported")
    digest = hmac.new(
        settings.jwt_secret_key.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64_encode(digest)


def _b64_encode(raw: bytes) -> str:
    """执行无 padding 的 base64url 编码。"""

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(data: str) -> str:
    """执行 JWT base64url 解码并返回 UTF-8 字符串。"""

    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii")).decode("utf-8")
