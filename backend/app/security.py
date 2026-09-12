"""认证内核：口令哈希与 JWT 签发/校验。

刻意只使用标准库（hashlib / hmac / secrets / json / base64）。这个项目的
AKShare 依赖链已经让装包变得昂贵，认证是最不该再引入第三方依赖的地方：
`passlib`、`bcrypt`、`PyJWT` 能做的事，标准库里都有等价实现。

口令存储格式为 self-describing 字符串，便于以后提升迭代次数而不破坏旧记录：

    pbkdf2_sha256$<iterations>$<salt-hex>$<digest-hex>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 240_000
PBKDF2_SALT_BYTES = 16

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "fund-compass"
JWT_LEEWAY_SECONDS = 5

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200
MAX_EMAIL_LENGTH = 254

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class TokenError(Exception):
    """令牌缺失、格式错误、过期或签名不匹配。"""


class PasswordPolicyError(ValueError):
    """口令不满足最小强度要求。"""


class EmailFormatError(ValueError):
    """邮箱格式不合法。"""


def normalize_email(raw: str) -> str:
    """统一大小写与首尾空白，保证 `A@b.com` 和 `a@b.com` 是同一个人。"""
    value = (raw or "").strip().lower()
    if not value:
        raise EmailFormatError("请输入邮箱")
    if len(value) > MAX_EMAIL_LENGTH:
        raise EmailFormatError("邮箱过长")
    if not _EMAIL_PATTERN.match(value):
        raise EmailFormatError("邮箱格式不正确")
    return value


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise PasswordPolicyError("口令必须是字符串")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"口令至少需要 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"口令不能超过 {MAX_PASSWORD_LENGTH} 位")
    if password.isspace():
        raise PasswordPolicyError("口令不能全部为空白字符")
    return password


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """恒定时间比对。任何解析失败都返回 False，绝不抛错给调用方。"""
    if not password or not encoded:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != PBKDF2_ALGORITHM:
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(expected.hex(), digest_hex)
    except (ValueError, TypeError, AttributeError):
        return False


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _compact_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def issue_token(
    subject: str,
    secret: str,
    *,
    ttl_seconds: int,
    issuer: str = JWT_ISSUER,
    now: float | None = None,
) -> tuple[str, int]:
    """签发 HS256 JWT，返回 (token, 过期时间戳)。"""
    if not subject:
        raise TokenError("令牌主题不能为空")
    if not secret:
        raise TokenError("缺少签名密钥")
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + max(int(ttl_seconds), 60)
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    payload = {"sub": subject, "iat": issued_at, "exp": expires_at, "iss": issuer}
    signing_input = f"{_b64url_encode(_compact_json(header))}.{_b64url_encode(_compact_json(payload))}"
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}", expires_at


def decode_token(
    token: str,
    secret: str,
    *,
    issuer: str = JWT_ISSUER,
    now: float | None = None,
    leeway_seconds: int = JWT_LEEWAY_SECONDS,
) -> dict[str, Any]:
    """校验签名与声明，返回 payload。任何异常都收敛为 TokenError。"""
    if not token or not secret:
        raise TokenError("缺少令牌或签名密钥")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("令牌格式不正确")
    signing_input = f"{parts[0]}.{parts[1]}"
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        provided_signature = _b64url_decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise TokenError("令牌无法解析") from exc
    if not isinstance(header, dict) or header.get("alg") != JWT_ALGORITHM:
        raise TokenError("令牌签名算法不受支持")
    if not isinstance(payload, dict):
        raise TokenError("令牌载荷不可用")
    expected_signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise TokenError("令牌签名校验失败")
    current = int(time.time() if now is None else now)
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        raise TokenError("令牌缺少过期时间")
    if current > expires_at + leeway_seconds:
        raise TokenError("登录状态已过期，请重新登录")
    if issuer and payload.get("iss") != issuer:
        raise TokenError("令牌签发方不匹配")
    if not str(payload.get("sub") or "").strip():
        raise TokenError("令牌主题为空")
    return payload


def load_or_create_secret(path: Path) -> str:
    """读取持久化签名密钥；不存在则生成。

    密钥必须落在磁盘上，否则进程每次重启都会让所有已登录用户掉线。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        value = secrets.token_urlsafe(48)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        # 只读文件系统等极端情况下退回进程内密钥：本次运行可用，重启后需重新登录。
        return secrets.token_urlsafe(48)


# ---------------------------------------------------------------------------
# 邀请码
# ---------------------------------------------------------------------------

MAX_INVITE_CODE_LENGTH = 200


def normalize_invite_code(raw: str) -> str:
    """归一化邀请码，容忍粘贴时带入的空白、分隔符与大小写差异。

    邀请码主要靠聊天工具口头或截图传递，很容易带上首尾空格，或者被自动
    排版成 `abcd-efgh` 这种分组写法。归一化之后再比对，能消除掉一大类
    「码明明是对的却进不去」的挫败感，且不牺牲任何强度。
    """
    if not isinstance(raw, str):
        return ""
    return re.sub(r"[\s\-_]+", "", raw).lower()


def match_invite_code(provided: str, expected: Iterable[str]) -> bool:
    """恒定时间比对邀请码。

    两个刻意的取舍：

    - 用 UTF-8 bytes 而不是 str 调 `hmac.compare_digest`。后者对 str 只接受
      ASCII 字符，邀请码里一旦出现中文会直接抛 TypeError。
    - 循环里不提前 return。命中即跳出会让响应时间随「匹配到的位置」变化，
      等于把邀请码逐位泄露出去。
    """
    candidate = normalize_invite_code(provided)
    if not candidate:
        return False
    matched = False
    for item in expected:
        target = normalize_invite_code(item)
        if not target:
            continue
        if hmac.compare_digest(candidate.encode("utf-8"), target.encode("utf-8")):
            matched = True
    return matched
