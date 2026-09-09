import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select

import app.db.session as session_module
from app.db.base import Base, FundProfileSnapshot, FundRecord, JobRun, OutboxEvent, RawDataSnapshot
from app.db.base import FundNavSnapshot
from app.db.nav_repository import NavSnapshotRepository
from app.db.snapshot_repository import SnapshotRepository
from app.models import Fund


def make_fund(code: str) -> Fund:
    return Fund(
        id=f"ak-{code}", code=code, name=f"测试基金 {code}", short_name=f"测试 {code}",
        type="混合型", risk="未获取", manager="未获取", manager_years=None, company="未获取",
        theme="未标注", nav=1.0, nav_date="2026-09-09", ytd=1.0, one_year=2.0,
        volatility=None, drawdown=None, fee=None, scale=None, inception=None, score=50,
        score_parts=[], reason="reference", caveat="待核验", highlights=[], status="待核",
        source="AKShare public reference", snapshot="rank", chart=[], tags=[], intake="未获取",
        quality_status="REFERENCE", nav_source_type="AKSHARE_PUBLIC", nav_trust_level="LOW",
        nav_freshness="REFERENCE",
    )


def test_persist_fund_universe_writes_snapshot_job_and_outbox(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def run():
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        result = await SnapshotRepository().persist_fund_universe(
            [make_fund("000001"), make_fund("000002")],
            source_name="AKShare public reference", source_type="AKSHARE_PUBLIC", trust_level="LOW",
        )
        duplicate = await SnapshotRepository().persist_fund_universe(
            [make_fund("000001"), make_fund("000002")],
            source_name="AKShare public reference", source_type="AKSHARE_PUBLIC", trust_level="LOW",
        )
        async with session_module.get_session_factory()() as session:
            assert result["updated"] == 2
            assert duplicate["status"] == "UNCHANGED"
            assert duplicate["snapshotId"] == result["snapshotId"]
            assert await session.scalar(select(func.count()).select_from(FundRecord)) == 2
            assert await session.scalar(select(func.count()).select_from(FundProfileSnapshot)) == 2
            assert await session.scalar(select(func.count()).select_from(RawDataSnapshot)) == 1
            assert await session.scalar(select(func.count()).select_from(JobRun).where(JobRun.status == "COMPLETED")) == 1
            assert await session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "pending")) == 1
        await session_module.get_engine().dispose()

    asyncio.run(run())


def test_persist_history_is_idempotent_and_keeps_nav_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def run():
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        fund = make_fund("000003")
        records = [
            {"date": "2026-09-08", "nav": 1.0, "dailyChange": 0.0},
            {"date": "2026-09-09", "nav": 1.1, "dailyChange": 10.0},
        ]
        first = await NavSnapshotRepository().persist_history(
            fund, records, source_name="AKShare public reference",
            source_type="AKSHARE_PUBLIC", trust_level="LOW",
        )
        duplicate = await NavSnapshotRepository().persist_history(
            fund, records, source_name="AKShare public reference",
            source_type="AKSHARE_PUBLIC", trust_level="LOW",
        )
        async with session_module.get_session_factory()() as session:
            assert first["updated"] == 2
            assert duplicate["status"] == "UNCHANGED"
            assert duplicate["snapshotId"] == first["snapshotId"]
            assert await session.scalar(select(func.count()).select_from(FundNavSnapshot)) == 2
        await session_module.get_engine().dispose()

    asyncio.run(run())
