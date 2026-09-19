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

# 平台给的 Postgres 连接串一律是裸 postgresql:// —— Neon、Render、Supabase
# 控制台复制出来的都是这个形式。而 create_async_engine 只认带驱动的
# postgresql+asyncpg://：拿到裸串它会去加载同步驱动 psycopg2，本项目没装，
# 于是服务启动直接崩在 ModuleNotFoundError；postgres://（Heroku 系）更早一步，
# 连方言插件都找不到。这里补齐驱动，让连接串可以原样粘贴。
_ASYNCPG_SCHEMES = ("postgresql://", "postgres://")

# libpq 的 query 参数名和 asyncpg 并不一致，而 SQLAlchemy 会把 URL 里的 query
# **原样**塞进 connect_args，于是 Neon 连接串自带的 sslmode=require 会变成
# TypeError: connect() got an unexpected keyword argument 'sslmode'。
_QUERY_RENAMES = {"sslmode": "ssl"}
# asyncpg 完全没有对应概念的参数，留着只会在连接时炸。
_QUERY_DROPPED = ("channel_binding", "gssencmode")

# asyncpg 默认每连接缓存 100 条预处理语句。直连时是纯收益，但连接池中间件
# （Neon 的 -pooler 端点、Supabase 的 6543 端口、pgbouncer 事务模式）会在
# 服务端切换后端连接，缓存的语句名随之冲突：
#     DuplicatePreparedStatementError: prepared statement "__asyncpg_stmt_1__" already exists
# 本应用每次请求只有个位数查询，瓶颈在行情抓取与模型调用（秒级），SQL 往返
# （微秒级）不是瓶颈，所以默认关掉缓存换兼容性。确实需要时在连接串里显式写
# prepared_statement_cache_size=100 即可恢复。
_QUERY_DEFAULTS = {"prepared_statement_cache_size": "0"}


def _normalize_database_url(value: str) -> str:
    for scheme in _ASYNCPG_SCHEMES:
        if value.startswith(scheme):
            value = "postgresql+asyncpg://" + value[len(scheme):]
            break
    if not value.startswith("postgresql+asyncpg://"):
        return value
    base, separator, query = value.partition("?")
    params: list[str] = []
    seen: set[str] = set()
    for chunk in query.split("&") if separator else []:
        if not chunk:
            continue
        key, _, raw = chunk.partition("=")
        if key in _QUERY_DROPPED:
            continue
        renamed = _QUERY_RENAMES.get(key, key)
        seen.add(renamed)
        params.append(f"{renamed}={raw}" if raw else renamed)
    for key, default in _QUERY_DEFAULTS.items():
        if key not in seen:
            params.append(f"{key}={default}")
    if not params:
        return base
    return base + "?" + "&".join(params)


def database_url() -> str:
    value = _normalize_database_url(str(config_value(
        "server", "database_url", "sqlite+aiosqlite:///./data/fund-compass.db",
        env_name="FUND_COMPASS_DATABASE_URL",
    )).strip())
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
