from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


SQLITE_PRAGMAS = (
    # 外键约束在 SQLite 里默认是**关**的，而且按连接生效，必须显式打开。
    # 模型里定义了 14 处 ForeignKey，此前全靠代码手工清理孤儿数据来维持 ——
    # 那是「靠自觉维持的平衡」，只要有一处漏掉就会静默留下脏数据。
    "PRAGMA foreign_keys=ON",
    # WAL：写操作不再锁整个库，读写可以并发，是 SQLite 官方对 Web 应用的推荐模式。
    # 单用户无感；多个用户同时跑研究（每次要写 conversation + run + steps）时差别明显。
    "PRAGMA journal_mode=WAL",
)


def _register_sqlite_pragmas(engine: AsyncEngine) -> None:
    """把 PRAGMA 挂到每条新连接上（SQLite 的这两个设置都不跨连接继承）。"""

    @event.listens_for(engine.sync_engine, "connect")
    def _apply(dbapi_connection, _record):  # pragma: no cover - 由连接池内部触发
        cursor = dbapi_connection.cursor()
        try:
            for statement in SQLITE_PRAGMAS:
                cursor.execute(statement)
        finally:
            cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        kwargs = {"pool_pre_ping": True}
        is_sqlite = database_url().startswith("sqlite")
        if is_sqlite:
            kwargs = {}
        _engine = create_async_engine(database_url(), **kwargs)
        if is_sqlite:
            _register_sqlite_pragmas(_engine)
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
    except Exception as exc:  # noqa: BLE001 - 驱动异常无法穷举；错误已写进返回值
        return {"status": "ERROR", "configured": True, "error": str(exc)[:200], "urlScheme": database_url().split(":", 1)[0]}
