from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import config_value


def database_url() -> str:
    value = str(config_value(
        "server", "database_url", "sqlite+aiosqlite:///./data/fund-compass.db",
        env_name="FUND_COMPASS_DATABASE_URL",
    )).strip()
    if value.startswith("sqlite+aiosqlite:///"):
        relative = value.removeprefix("sqlite+aiosqlite:///")
        if relative and relative != ":memory:" and not relative.startswith("/"):
            Path(relative).parent.mkdir(parents=True, exist_ok=True)
    return value


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        kwargs = {"pool_pre_ping": True}
        if database_url().startswith("sqlite"):
            kwargs = {}
        _engine = create_async_engine(database_url(), **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session


async def database_health() -> dict[str, object]:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"status": "ACTIVE", "configured": True, "urlScheme": database_url().split(":", 1)[0]}
    except Exception as exc:  # pragma: no cover - depends on deployment database
        return {"status": "ERROR", "configured": True, "error": str(exc)[:200], "urlScheme": database_url().split(":", 1)[0]}
