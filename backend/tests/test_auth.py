"""认证内核与登录接口的回归测试。

这里锁定的关键不变量：口令永远不以明文存储、令牌被篡改或过期后必须失效、
以及「账号是否存在」不会从登录失败响应里泄漏出去。
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import (
    EmailFormatError,
    PasswordPolicyError,
    TokenError,
    decode_token,
    hash_password,
    issue_token,
    normalize_email,
    verify_password,
)

client = TestClient(app)
SECRET = "test-secret-not-for-production"
# 由 conftest 固定注入，见 tests/conftest.py。注册用例必须带上它，
# 否则会停在邀请码这一关，测不到后面的口令/邮箱校验。
INVITE_CODE = os.environ["FUND_COMPASS_INVITE_CODE"]


def unique_email() -> str:
    return f"auth_{uuid.uuid4().hex[:10]}@example.com"


# ---------------------------------------------------------------------------
# 口令哈希
# ---------------------------------------------------------------------------

def test_password_hash_is_salted_and_verifiable():
    encoded = hash_password("correct horse battery")
    assert "correct horse battery" not in encoded
    assert encoded.startswith("pbkdf2_sha256$")
    # 同一口令两次哈希必须不同（盐随机）。
    assert encoded != hash_password("correct horse battery")
    assert verify_password("correct horse battery", encoded) is True
    assert verify_password("wrong password", encoded) is False


def test_verify_password_never_raises_on_garbage():
    for broken in ["", "not-a-hash", "pbkdf2_sha256$abc$def$ghi", "md5$1$aa$bb", None]:
        assert verify_password("whatever", broken) is False  # type: ignore[arg-type]


def test_weak_passwords_are_rejected():
    with pytest.raises(PasswordPolicyError):
        hash_password("short")
    with pytest.raises(PasswordPolicyError):
        hash_password("         ")


def test_email_normalization_rejects_malformed_values():
    assert normalize_email("  User@Example.COM ") == "user@example.com"
    for bad in ["", "no-at-sign", "two@@at.com", "trailing@dot."]:
        with pytest.raises(EmailFormatError):
            normalize_email(bad)


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------

def test_token_round_trip_and_tamper_detection():
    token, expires_at = issue_token("usr_1", SECRET, ttl_seconds=600)
    payload = decode_token(token, SECRET)
    assert payload["sub"] == "usr_1"
    assert payload["exp"] == expires_at

    # 篡改载荷后签名必须失配。
    header, body, signature = token.split(".")
    forged_body = body[:-2] + ("AA" if not body.endswith("AA") else "BB")
    with pytest.raises(TokenError):
        decode_token(f"{header}.{forged_body}.{signature}", SECRET)
    # 换密钥也必须失败。
    with pytest.raises(TokenError):
        decode_token(token, "another-secret")


def test_expired_token_is_rejected():
    token, _ = issue_token("usr_1", SECRET, ttl_seconds=600, now=1_000_000)
    assert decode_token(token, SECRET, now=1_000_100)["sub"] == "usr_1"
    with pytest.raises(TokenError):
        decode_token(token, SECRET, now=1_000_000 + 601 + 60)


def test_malformed_tokens_are_rejected():
    for broken in ["", "a.b", "a.b.c.d", "not.a.token"]:
        with pytest.raises(TokenError):
            decode_token(broken, SECRET)


# ---------------------------------------------------------------------------
# 注册 / 登录接口
# ---------------------------------------------------------------------------

def test_register_returns_a_usable_session():
    email = unique_email()
    response = client.post("/api/auth/register", json={
        "email": email, "password": "test-password-123", "displayName": "何总",
        "inviteCode": INVITE_CODE,
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["tokenType"] == "Bearer"
    assert body["user"]["email"] == email.lower()
    assert body["user"]["displayName"] == "何总"
    assert "password" not in response.text and "passwordHash" not in response.text

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["user"]["email"] == email.lower()


def test_register_rejects_duplicate_email():
    email = unique_email()
    payload = {"email": email, "password": "test-password-123", "inviteCode": INVITE_CODE}
    assert client.post("/api/auth/register", json=payload).status_code == 201
    duplicate = client.post("/api/auth/register", json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "EMAIL_ALREADY_REGISTERED"


def test_register_rejects_weak_password_and_bad_email():
    weak = client.post("/api/auth/register", json={
        "email": unique_email(), "password": "123", "inviteCode": INVITE_CODE})
    assert weak.status_code == 400
    assert weak.json()["detail"]["code"] == "WEAK_PASSWORD"
    bad = client.post("/api/auth/register", json={
        "email": "not-an-email", "password": "test-password-123", "inviteCode": INVITE_CODE})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "INVALID_EMAIL"


def test_login_does_not_leak_whether_account_exists():
    email = unique_email()
    client.post("/api/auth/register", json={
        "email": email, "password": "test-password-123", "inviteCode": INVITE_CODE})

    ok = client.post("/api/auth/login", json={"email": email, "password": "test-password-123"})
    assert ok.status_code == 200

    wrong = client.post("/api/auth/login", json={"email": email, "password": "not-the-password"})
    missing = client.post("/api/auth/login", json={"email": unique_email(), "password": "test-password-123"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"]["code"] == missing.json()["detail"]["code"] == "INVALID_CREDENTIALS"


def test_me_and_logout_require_a_valid_token():
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert client.post("/api/auth/logout").status_code == 401

    token = client.post("/api/auth/register", json={
        "email": unique_email(), "password": "test-password-123", "inviteCode": INVITE_CODE,
    }).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 204


def test_login_is_case_insensitive_on_email():
    email = unique_email()
    client.post("/api/auth/register", json={
        "email": email, "password": "test-password-123", "inviteCode": INVITE_CODE})
    response = client.post("/api/auth/login", json={
        "email": email.upper(), "password": "test-password-123",
    })
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------

def register_headers(prefix: str = "cnv") -> dict[str, str]:
    token = client.post("/api/auth/register", json={
        "email": f"{prefix}_{uuid.uuid4().hex[:10]}@example.com",
        "password": "test-password-123",
        "inviteCode": INVITE_CODE,
    }).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_conversation_crud_and_ownership():
    owner = register_headers("owner")
    stranger = register_headers("stranger")

    created = client.post("/api/conversations", json={"title": "新能源定投"}, headers=owner)
    assert created.status_code == 201
    conversation_id = created.json()["id"]
    assert conversation_id.startswith("cnv_")

    listed = client.get("/api/conversations", headers=owner).json()["items"]
    assert [item["id"] for item in listed] == [conversation_id]

    # 他人既读不到也删不掉。
    assert client.get(f"/api/conversations/{conversation_id}", headers=stranger).status_code == 404
    assert client.delete(f"/api/conversations/{conversation_id}", headers=stranger).status_code == 404
    assert client.get("/api/conversations", headers=stranger).json()["items"] == []

    assert client.delete(f"/api/conversations/{conversation_id}", headers=owner).status_code == 204
    assert client.get(f"/api/conversations/{conversation_id}", headers=owner).status_code == 404


def test_conversations_require_login():
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/conversations", json={"title": "x"}).status_code == 401
