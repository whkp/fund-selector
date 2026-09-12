"""用户与对话的持久化，以及 FastAPI 的登录态依赖。

这一层把「谁在调用」变成一个有类型的对象，让路由可以像读 `request.state`
一样自然地拿到当前用户，而不必在每个端点里手写解析令牌的样板代码。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from .config import config_value
from .db.base import Base, Conversation, ConversationMessage, UserRecord
from .db.session import get_engine, get_session_factory
from .security import (
    EmailFormatError,
    TokenError,
    decode_token,
    hash_password,
    issue_token,
    load_or_create_secret,
    normalize_email,
    validate_password,
    verify_password,
)

DEFAULT_TOKEN_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_SECRET_FILE = Path(__file__).resolve().parents[1] / "data" / ".jwt-secret"
MAX_TITLE_LENGTH = 120


class EmailAlreadyRegistered(Exception):
    """该邮箱已经注册过。"""


class ConversationNotFound(Exception):
    """对话不存在，或不属于当前用户。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def token_ttl_seconds() -> int:
    raw = str(config_value(
        "security", "token_ttl_seconds", DEFAULT_TOKEN_TTL_SECONDS,
        env_name="FUND_COMPASS_JWT_TTL_SECONDS",
    )).strip()
    try:
        return max(int(raw), 300)
    except ValueError:
        return DEFAULT_TOKEN_TTL_SECONDS


def jwt_secret() -> str:
    configured = str(config_value(
        "security", "jwt_secret", "", env_name="FUND_COMPASS_JWT_SECRET"
    )).strip()
    if configured:
        return configured
    raw_path = str(config_value(
        "security", "secret_file", str(DEFAULT_SECRET_FILE),
        env_name="FUND_COMPASS_JWT_SECRET_FILE",
    )).strip()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return load_or_create_secret(path)


def _patch_legacy_users_table(sync_connection: Any) -> None:
    """给历史 `users` 表补上认证相关列。

    最初的 schema 只声明了 `subject`/`role`，没有任何登录字段。用 Alembic
    迁移是正路，但直接 `python run.py` 的部署不会跑迁移，所以这里做一次
    幂等自愈，让旧库也能开箱即用。SQLite 的 `ALTER TABLE ADD COLUMN` 不需要
    重建表，成本可忽略。
    """
    inspector = inspect(sync_connection)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    additions = {
        "email": "VARCHAR(254)",
        "display_name": "VARCHAR(80)",
        "password_hash": "VARCHAR(255)",
        "is_active": "BOOLEAN",
        "last_login_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    for name, ddl in additions.items():
        if name not in existing:
            sync_connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
    sync_connection.execute(text("UPDATE users SET email = subject WHERE email IS NULL OR email = ''"))
    sync_connection.execute(text("UPDATE users SET display_name = email WHERE display_name IS NULL"))
    sync_connection.execute(text("UPDATE users SET role = 'user' WHERE role IS NULL"))
    sync_connection.execute(text("UPDATE users SET is_active = 1 WHERE is_active IS NULL"))


async def ensure_auth_schema() -> None:
    """启动时建表。与正式的 Alembic 迁移并存，二者结果一致。"""
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_patch_legacy_users_table)


def user_public(user: UserRecord) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "displayName": user.display_name or user.email,
        "role": user.role,
        "createdAt": user.created_at.isoformat() if user.created_at else None,
        "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
    }


async def create_user(
    email: str,
    password: str,
    display_name: str = "",
    *,
    role: str = "user",
) -> UserRecord:
    normalized = normalize_email(email)
    validate_password(password)
    now = _utcnow()
    record = UserRecord(
        id=f"usr_{uuid.uuid4().hex[:16]}",
        subject=normalized,
        email=normalized,
        display_name=(display_name or "").strip()[:80] or normalized.split("@")[0][:80],
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )
    try:
        async with get_session_factory()() as session:
            session.add(record)
            await session.commit()
    except IntegrityError as exc:
        raise EmailAlreadyRegistered(normalized) from exc
    return record


async def authenticate(email: str, password: str) -> UserRecord | None:
    """校验凭据。所有失败路径统一返回 None，不泄漏账号是否存在。"""
    try:
        normalized = normalize_email(email)
    except EmailFormatError:
        return None
    async with get_session_factory()() as session:
        user = await session.scalar(select(UserRecord).where(UserRecord.email == normalized))
        if user is None or not user.is_active:
            return None
        if not verify_password(password or "", user.password_hash or ""):
            return None
        user.last_login_at = _utcnow()
        user.updated_at = user.last_login_at
        await session.commit()
        return user


async def get_user(user_id: str) -> UserRecord | None:
    async with get_session_factory()() as session:
        return await session.scalar(select(UserRecord).where(UserRecord.id == user_id))


def issue_session_token(user: UserRecord) -> tuple[str, int]:
    """`sub` 用不可变的用户主键。

    `subject` 是「外部身份标识」的占位（当前等于邮箱），将来接入 SSO 或允许
    改邮箱时会变；把它写进令牌会让所有已签发的登录态突然失效，所以令牌里
    只放 `users.id`。
    """
    return issue_token(user.id, jwt_secret(), ttl_seconds=token_ttl_seconds())


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


async def optional_user(request: Request) -> UserRecord | None:
    """令牌可选：用于「登录后看到自己的数据，未登录也能浏览」的读接口。"""
    token = _extract_bearer(request)
    if not token:
        return None
    try:
        payload = decode_token(token, jwt_secret())
    except TokenError:
        return None
    user = await get_user(str(payload.get("sub") or ""))
    return user if user and user.is_active else None


async def current_user(request: Request) -> UserRecord:
    """强制登录。失败时返回 401，前端据此跳转登录页。"""
    token = _extract_bearer(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "请先登录"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(token, jwt_secret())
    except TokenError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_TOKEN", "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    user = await get_user(str(payload.get("sub") or ""))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail={"code": "ACCOUNT_UNAVAILABLE", "message": "账号不存在或已停用"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------

def _conversation_public(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "status": conversation.status,
        "messageCount": conversation.message_count,
        "createdAt": conversation.created_at.isoformat() if conversation.created_at else None,
        "updatedAt": conversation.updated_at.isoformat() if conversation.updated_at else None,
        "lastMessageAt": conversation.last_message_at.isoformat() if conversation.last_message_at else None,
    }


def _message_public(message: ConversationMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "runId": message.run_id,
        "payload": message.payload,
        "createdAt": message.created_at.isoformat() if message.created_at else None,
    }


def _title_from(content: str) -> str:
    compact = " ".join((content or "").split())
    return compact[:MAX_TITLE_LENGTH] or "新的研究对话"


async def create_conversation(user_id: str, title: str = "") -> dict[str, Any]:
    now = _utcnow()
    record = Conversation(
        id=f"cnv_{uuid.uuid4().hex[:16]}",
        user_id=user_id,
        title=(title or "").strip()[:MAX_TITLE_LENGTH] or "新的研究对话",
        status="ACTIVE",
        message_count=0,
        last_message_at=None,
        created_at=now,
        updated_at=now,
    )
    async with get_session_factory()() as session:
        session.add(record)
        await session.commit()
    return _conversation_public(record)


async def list_conversations(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with get_session_factory()() as session:
        result = await session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .limit(max(min(limit, 200), 1))
        )
        return [_conversation_public(item) for item in result.all()]


async def _load_conversation(session: Any, user_id: str, conversation_id: str) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:
        raise ConversationNotFound(conversation_id)
    return conversation


async def get_conversation(user_id: str, conversation_id: str) -> dict[str, Any]:
    async with get_session_factory()() as session:
        conversation = await _load_conversation(session, user_id, conversation_id)
        return _conversation_public(conversation)


async def list_messages(user_id: str, conversation_id: str, limit: int = 200) -> list[dict[str, Any]]:
    async with get_session_factory()() as session:
        await _load_conversation(session, user_id, conversation_id)
        result = await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc())
            .limit(max(min(limit, 500), 1))
        )
        return [_message_public(item) for item in result.all()]


async def append_message(
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
    *,
    run_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utcnow()
    async with get_session_factory()() as session:
        conversation = await _load_conversation(session, user_id, conversation_id)
        message = ConversationMessage(
            id=f"msg_{uuid.uuid4().hex[:16]}",
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            content=content or "",
            run_id=run_id,
            payload=payload,
            created_at=now,
        )
        session.add(message)
        conversation.message_count = (conversation.message_count or 0) + 1
        conversation.last_message_at = now
        conversation.updated_at = now
        # 首条用户消息决定标题，避免列表里全是「新的研究对话」。
        if role == "user" and conversation.message_count == 1:
            conversation.title = _title_from(content)
        await session.commit()
        return _message_public(message)


async def delete_conversation(user_id: str, conversation_id: str) -> None:
    async with get_session_factory()() as session:
        conversation = await _load_conversation(session, user_id, conversation_id)
        for message in await session.scalars(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation_id
            )
        ):
            await session.delete(message)
        await session.delete(conversation)
        await session.commit()


async def conversation_for_run(user_id: str, run_id: str) -> dict[str, Any] | None:
    """按研究记录 ID 反查所属对话，供「复盘日志」直接跳转。"""
    async with get_session_factory()() as session:
        message = await session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.user_id == user_id, ConversationMessage.run_id == run_id
            )
        )
        if message is None:
            return None
        conversation = await _load_conversation(session, user_id, message.conversation_id)
        return _conversation_public(conversation)


async def find_message_by_run(user_id: str, run_id: str) -> dict[str, Any] | None:
    """进程重启后，内存里的研究记录会丢；这里从对话消息里把它捞回来。"""
    async with get_session_factory()() as session:
        message = await session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.user_id == user_id, ConversationMessage.run_id == run_id
            )
        )
        return _message_public(message) if message is not None else None


__all__ = [
    "ConversationNotFound",
    "EmailAlreadyRegistered",
    "append_message",
    "authenticate",
    "conversation_for_run",
    "create_conversation",
    "create_user",
    "current_user",
    "delete_conversation",
    "ensure_auth_schema",
    "find_message_by_run",
    "get_conversation",
    "get_user",
    "issue_session_token",
    "jwt_secret",
    "list_conversations",
    "list_messages",
    "optional_user",
    "token_ttl_seconds",
    "user_public",
]
