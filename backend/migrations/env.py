from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db import base  # noqa: F401
from app.db.base import Base
from app.db.session import database_url

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers 默认是 True：alembic.ini 只声明 root/sqlalchemy/alembic
    # 三个 logger，fileConfig 会把其余全部已存在的 logger（app.*、uvicorn.*…）
    # 就地禁用。启动路径会跑 ensure_schema，禁掉之后所有应用日志（预热装载、
    # 未处理异常、访问日志）全部静默——只能在“服务其实好着”的情况下假装哑巴。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# 复用 session.database_url()，而不是在这里自己再读一次 config_value ——
# 那个函数会把平台给的裸 postgresql:// 补成 postgresql+asyncpg://。
# 两边各读一次的话逻辑会漂移：应用连得上、迁移却崩在找不到 psycopg2，
# 而迁移跑在启动路径上，等于整个服务起不来。
DATABASE_URL = database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = DATABASE_URL
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
