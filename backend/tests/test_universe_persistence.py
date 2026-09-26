"""目录的落库与装载：refresh_funds → 后台快照；启动 adopt_universe ← 数据库。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

import app.db.session as session_module
from app.db.base import Base, FundRecord, RawDataSnapshot
from app.db.snapshot_repository import (
    SnapshotRepository,
    fund_payload,
    load_fund_universe,
)
from app.models import Fund, SourceStatus
from app.providers import DataRepository


def make_fund(code: str, name: str = "", **overrides) -> Fund:
    values = dict(
        id=f"ak-{code}", code=code, name=name or f"测试基金{code}",
        short_name=(name or f"测试{code}")[:16], type="混合型", risk="未获取",
        manager="未获取", manager_years=None, company="未获取", theme="未标注",
        nav=1.0, nav_date="2026-09-25", ytd=None, one_year=None, volatility=None,
        drawdown=None, fee=None, scale=None, inception=None, score=50, score_parts=[],
        reason="", caveat="", highlights=[], status="待核", source="AKShare public reference",
        snapshot="rank", chart=[], tags=[], intake="未获取", quality_status="REFERENCE",
    )
    values.update(overrides)
    return Fund(**values)


class StubProvider:
    """带 status 的最小 provider 替身（落库任务会读 provider.status）。"""

    def __init__(self, funds: list[Fund] | None = None) -> None:
        self.funds = funds if funds is not None else [make_fund("000001")]
        self.status = SourceStatus(
            source_name="AKShare public reference", source_type="AKSHARE_PUBLIC",
            trust_level="LOW", status="ACTIVE", configured=True,
            license_scope="development/reference only", field_coverage=[],
        )

    async def list_funds(self) -> list[Fund]:
        return self.funds


def test_adopt_universe_replaces_memory_and_sets_freshness():
    async def scenario() -> None:
        repository = DataRepository(StubProvider())
        funds = [
            make_fund("161725", name="招商中证白酒指数A", theme="中证白酒指数"),
            make_fund("000001", name="华夏成长混合"),
        ]
        await repository.adopt_universe(funds, datetime.now(UTC))
        assert len(repository.funds) == 2
        assert repository.get_fund("161725").name == "招商中证白酒指数A"
        # n-gram 索引必须覆盖新装进来的基金，否则研究检索是盲的。
        assert repository.relevance_scores("白酒").get("161725")
        assert repository._funds_are_fresh()

        # 快照时间决定 TTL：10 小时前的快照视为过期，交给 ensure_funds 刷新。
        await repository.adopt_universe(funds, datetime.now(UTC) - timedelta(hours=10))
        assert not repository._funds_are_fresh()

        # naive 快照时间（SQLite 读回来的形态）按 UTC 解读，不能与 aware now 相减时炸掉。
        await repository.adopt_universe(funds, datetime(2026, 9, 26, 0, 0))
        assert repository._funds_fetched_at is not None
        assert repository._funds_fetched_at.tzinfo is not None

    asyncio.run(scenario())


def test_refresh_funds_persists_universe_snapshot(tmp_path, monkeypatch):
    """刷新成功后，后台任务把目录落成 raw 快照，并能被装载路径完整读回。"""
    db_path = tmp_path / "universe.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def scenario() -> None:
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        repository = DataRepository(
            StubProvider([make_fund("000001"), make_fund("000002", name="沪深300指数基金")])
        )
        assert await repository.refresh_funds() is True
        task = repository._persist_task
        assert task is not None
        await task
        async with session_module.get_session_factory()() as session:
            assert await session.scalar(select(func.count()).select_from(FundRecord)) == 2
            snapshots = await session.scalar(
                select(func.count()).select_from(RawDataSnapshot).where(
                    RawDataSnapshot.endpoint == "fund-universe",
                )
            )
            assert snapshots == 1
        loaded = await load_fund_universe()
        assert loaded is not None
        funds, fetched_at = loaded
        assert [f.code for f in funds] == ["000001", "000002"]
        assert funds[1].name == "沪深300指数基金"
        assert fetched_at is not None
        await session_module.get_engine().dispose()

    asyncio.run(scenario())


def _legacy_raw_text(funds: list[Fund]) -> str:
    """旧格式（无压缩列）快照的明文 payload，hash 口径与 persist 一致。"""
    return json.dumps(
        [fund_payload(fund) for fund in funds],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )


def test_legacy_plaintext_snapshot_falls_back_and_gets_compressed(tmp_path, monkeypatch):
    """旧库只存明文快照：装载要能回退读它；同内容再次落库时就地补压缩副本。"""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def scenario() -> None:
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        funds = [make_fund("000001"), make_fund("000002")]
        raw_text = _legacy_raw_text(funds)
        async with session_module.get_session_factory()() as session, session.begin():
            session.add(RawDataSnapshot(
                id="raw_legacy", source_name="AKShare public reference",
                endpoint="fund-universe", fetched_at=datetime.now(UTC),
                business_date=datetime.now(UTC).date(),
                content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                payload_text=raw_text, parser_version="akshare-rank-v1", http_status=200,
            ))

        # 回退路径：只有明文也要能装载。
        loaded = await load_fund_universe()
        assert loaded is not None
        assert [f.code for f in loaded[0]] == ["000001", "000002"]

        # 同内容落库：应就地升级（补 gz、清明文），而不是报 UNCHANGED。
        result = await SnapshotRepository().persist_fund_universe(
            funds, source_name="AKShare public reference",
            source_type="AKSHARE_PUBLIC", trust_level="LOW",
        )
        assert result["status"] == "COMPRESSED"
        async with session_module.get_session_factory()() as session:
            row = await session.get(RawDataSnapshot, "raw_legacy")
            assert row is not None and row.payload_gz is not None and row.payload_text is None

        # 升级后装载仍正确（走 gz 路径）。
        loaded = await load_fund_universe()
        assert loaded is not None
        assert [f.code for f in loaded[0]] == ["000001", "000002"]
        await session_module.get_engine().dispose()

    asyncio.run(scenario())
