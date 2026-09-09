import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import app.db.session as session_module
from app.db.base import Base
from app.db.session import database_health


def test_database_health_and_metadata_create(tmp_path, monkeypatch):
    db_path = tmp_path / "fund-compass.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def run():
        health = await database_health()
        assert health["status"] == "ACTIVE"
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        assert {"funds", "fund_nav_snapshots", "fund_metric_snapshots", "outbox_events"}.issubset(tables)
        await session_module.get_engine().dispose()

    asyncio.run(run())


def test_initial_migration_upgrade_and_downgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "migration.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert db_path.exists()
