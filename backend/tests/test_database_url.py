"""连接串规范化：平台给的 Postgres URL 必须能原样粘贴。

用例对应实测出来的三种崩溃方式，都不是假想：

1. 裸 ``postgresql://``  -> ``ModuleNotFoundError: No module named 'psycopg2'``
   （SQLAlchemy 拿不到驱动名就去加载同步驱动，本项目没装）
2. ``postgres://``        -> ``NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres``
3. ``?sslmode=require``   -> ``TypeError: connect() got an unexpected keyword argument 'sslmode'``
   （SQLAlchemy 把 URL 的 query 原样塞进 connect_args，而 asyncpg 的参数叫 ssl）

前两种发生在建引擎时，也就是服务启动阶段 —— 不是某次查询失败，是整个服务起不来。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine

from app.db import session as session_module

CACHE = "prepared_statement_cache_size=0"

PLATFORM_URLS = [
    "postgresql://u:p@host:5432/db",
    "postgres://u:p@host:5432/db",
    "postgresql://u:p@host/db?sslmode=require",
    "postgresql://u:p@host/db?sslmode=require&channel_binding=require",
    "postgresql://u:p@host/db?sslmode=require&application_name=fund-compass",
]


def test_every_platform_url_yields_a_usable_async_engine() -> None:
    """验收标准：规范化之后 create_async_engine 必须能建起来。"""

    for raw in PLATFORM_URLS:
        normalized = session_module._normalize_database_url(raw)
        assert normalized.startswith("postgresql+asyncpg://"), raw
        engine = create_async_engine(normalized)
        assert engine.dialect.is_async is True, raw


def test_sslmode_is_renamed_to_asyncpg_spelling() -> None:
    normalized = session_module._normalize_database_url("postgresql://u:p@h/db?sslmode=require")
    assert normalized == f"postgresql+asyncpg://u:p@h/db?ssl=require&{CACHE}"


def test_libpq_only_params_are_dropped() -> None:
    normalized = session_module._normalize_database_url(
        "postgresql://u:p@h/db?sslmode=require&channel_binding=require"
    )
    assert "channel_binding" not in normalized
    assert normalized == f"postgresql+asyncpg://u:p@h/db?ssl=require&{CACHE}"


def test_unrelated_params_survive() -> None:
    normalized = session_module._normalize_database_url(
        "postgresql://u:p@h/db?sslmode=require&application_name=fund-compass"
    )
    assert normalized == (
        f"postgresql+asyncpg://u:p@h/db?ssl=require&application_name=fund-compass&{CACHE}"
    )


def test_statement_cache_is_disabled_for_pooler_compatibility() -> None:
    """Neon 的 -pooler 端点 / pgbouncer 不支持服务端预处理语句缓存。"""

    normalized = session_module._normalize_database_url("postgresql://u:p@h/db")
    assert "prepared_statement_cache_size=0" in normalized


def test_explicit_cache_size_is_respected() -> None:
    normalized = session_module._normalize_database_url(
        "postgresql://u:p@h/db?prepared_statement_cache_size=100"
    )
    assert normalized.count("prepared_statement_cache_size") == 1
    assert "prepared_statement_cache_size=100" in normalized


def test_url_with_correct_driver_is_unchanged() -> None:
    raw = "postgresql+asyncpg://u:p@h/db?ssl=require&prepared_statement_cache_size=0"
    assert session_module._normalize_database_url(raw) == raw


def test_sqlite_url_is_untouched() -> None:
    raw = "sqlite+aiosqlite:///./data/fund-compass.db"
    assert session_module._normalize_database_url(raw) == raw


def test_database_url_reads_env_and_normalizes(monkeypatch) -> None:
    monkeypatch.setenv(
        "FUND_COMPASS_DATABASE_URL",
        "postgresql://neondb_owner:npg_secret@ep-cool-1234-pooler.us-east-1.aws.neon.tech"
        "/neondb?sslmode=require",
    )
    assert session_module.database_url() == (
        "postgresql+asyncpg://neondb_owner:npg_secret@"
        "ep-cool-1234-pooler.us-east-1.aws.neon.tech/neondb?"
        "ssl=require&prepared_statement_cache_size=0"
    )
