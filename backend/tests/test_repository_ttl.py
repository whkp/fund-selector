"""DataRepository 的 TTL 行为。

回归背景：ensure_funds() 曾经写作 `bool(self.funds) or await self.refresh_funds()`，
只要内存非空就永久返回首个快照，导致进程存活期间数据完全冻结。这些测试锁定
「缓存新鲜时不打上游、过期后必须重新拉取、上游失败时降级为旧快照并推迟重试」
三条不变量。

（2026-09-26）历史净值加了「按需落库 + 数据库回收」：内存未命中且 1年 窗口时
先读快照，新鲜就回填。涉及回收的用例必须先把全局测试库里的历史快照清干净，
否则「未命中」分支不成立、上游调用计数会随测试顺序漂移。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

import app.db.session as session_module
from app.db.base import Base, FundNavSnapshot, RawDataSnapshot
from app.db.nav_repository import NavSnapshotRepository
from app.models import Fund
from app.providers import DataRepository


def make_fund(code: str, score: int = 50) -> Fund:
    return Fund(
        id=f"test-{code}", code=code, name=f"测试基金{code}", short_name=f"测试{code}",
        type="混合型", risk="未获取", manager="未获取", manager_years=None, company="未获取",
        theme="未标注", nav=1.0, nav_date="2026-09-10", ytd=None, one_year=None,
        volatility=None, drawdown=None, fee=None, scale=None, inception=None,
        score=score, score_parts=[], reason="", caveat="", highlights=[], status="待核",
        source="test", snapshot="test", chart=[], tags=[], intake="未获取",
        quality_status="REFERENCE",
    )


class FakeProvider:
    """记录调用次数的最小 provider 替身。"""

    def __init__(self, *, funds=None, history=None, fail: bool = False) -> None:
        self.funds = funds if funds is not None else [make_fund("000001")]
        self.history_records = history if history is not None else [
            {"date": "2026-09-10", "nav": 1.0, "dailyChange": 0.0}
        ]
        self.fail = fail
        self.list_calls = 0
        self.history_calls = 0

    async def list_funds(self):
        self.list_calls += 1
        return [] if self.fail else self.funds

    async def history(self, code, period):
        self.history_calls += 1
        return [] if self.fail else self.history_records


async def _drain_persist(repository: DataRepository) -> None:
    """落库是 refresh_funds 起的后台任务；不等它结束，asyncio.run 关闭
    event loop 时会留下 "Task was destroyed but it is pending!" 警告。"""
    task = repository._persist_task
    if task is not None and not task.done():
        await task


async def _drain_history_persists(repository: DataRepository) -> None:
    """同上，等所有按需落库的历史净值任务结束。"""
    tasks = list(repository._history_persist_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _clear_history_snapshots() -> None:
    """回收路径会先读数据库；清掉历史快照，保证「未命中」分支确定成立。"""
    async with session_module.get_session_factory()() as session, session.begin():
        await session.execute(delete(RawDataSnapshot).where(
            RawDataSnapshot.endpoint.like("fund-history:%")))
    await session_module.get_engine().dispose()


def test_ensure_funds_refetches_after_ttl_expires():
    async def scenario() -> None:
        provider = FakeProvider()
        repository = DataRepository(provider)
        repository.funds_ttl_seconds = 3600

        assert await repository.ensure_funds() is True
        assert provider.list_calls == 1

        # TTL 内重复调用不应再访问上游。
        assert await repository.ensure_funds() is True
        assert provider.list_calls == 1

        # 过期后必须重新拉取。
        repository._funds_fetched_at = datetime.now(UTC) - timedelta(seconds=3601)
        assert await repository.ensure_funds() is True
        assert provider.list_calls == 2
        await _drain_persist(repository)

    asyncio.run(scenario())


def test_ensure_funds_serves_stale_snapshot_when_upstream_fails():
    async def scenario() -> None:
        provider = FakeProvider()
        repository = DataRepository(provider)
        await repository.ensure_funds()
        assert provider.list_calls == 1

        provider.fail = True
        repository._funds_fetched_at = datetime.now(UTC) - timedelta(seconds=100_000)
        assert await repository.ensure_funds() is True
        assert provider.list_calls == 2
        # 上游失败时旧快照仍可服务。
        assert len(repository.list_funds()) == 1
        # 失败后推迟重试，不应每个请求都打上游。
        assert await repository.ensure_funds() is True
        assert provider.list_calls == 2
        await _drain_persist(repository)

    asyncio.run(scenario())


def test_history_refetches_after_ttl_expires():
    async def scenario() -> None:
        await _clear_history_snapshots()
        provider = FakeProvider()
        repository = DataRepository(provider)
        repository.history_ttl_seconds = 3600

        await repository.history("000001", "1年")
        await repository.history("000001", "1年")
        assert provider.history_calls == 1

        repository._history_fetched_at[("000001", "1年")] = (
            datetime.now(UTC) - timedelta(seconds=3601)
        )
        await repository.history("000001", "1年")
        assert provider.history_calls == 2

    asyncio.run(scenario())


def test_funds_refresh_clears_history_cache():
    async def scenario() -> None:
        await _clear_history_snapshots()
        provider = FakeProvider()
        repository = DataRepository(provider)

        await repository.ensure_funds()
        # 落库任务与后续写库串行执行，避免两个任务并发写同一个 SQLite 库。
        await _drain_persist(repository)
        await repository.history("000001", "1年")
        await _drain_history_persists(repository)
        assert ("000001", "1年") in repository.histories

        await repository.refresh_funds()
        assert repository.histories == {}
        assert repository._history_fetched_at == {}
        await _drain_persist(repository)

    asyncio.run(scenario())


def test_history_recovers_from_snapshot_without_hitting_provider(tmp_path, monkeypatch):
    """库里有新鲜的 1年 快照：内存未命中时直接回收，不打上游。"""
    db_path = tmp_path / "recover.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def scenario() -> None:
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        records = [{"date": datetime.now(UTC).date().isoformat(), "nav": 1.234,
                    "dailyChange": 0.5}]
        await NavSnapshotRepository().persist_history(
            make_fund("000001"), records, source_name="AKShare public reference",
            source_type="AKSHARE_PUBLIC", trust_level="LOW",
        )

        provider = FakeProvider()
        repository = DataRepository(provider)
        recovered = await repository.history("000001", "1年")

        assert provider.history_calls == 0
        assert recovered == records
        assert repository.histories[("000001", "1年")] == records
        await session_module.get_engine().dispose()

    asyncio.run(scenario())


def test_history_skips_stale_snapshot_and_refetches(tmp_path, monkeypatch):
    """快照过期（business_date 距今超过阈值）：拒绝回收，照常打上游。"""
    db_path = tmp_path / "stale.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def scenario() -> None:
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        stale_date = (datetime.now(UTC).date() - timedelta(days=10)).isoformat()
        await NavSnapshotRepository().persist_history(
            make_fund("000001"), [{"date": stale_date, "nav": 0.5, "dailyChange": 0.0}],
            source_name="AKShare public reference",
            source_type="AKSHARE_PUBLIC", trust_level="LOW",
        )

        provider = FakeProvider()
        repository = DataRepository(provider)
        records = await repository.history("000001", "1年")

        assert provider.history_calls == 1
        assert records == provider.history_records
        await session_module.get_engine().dispose()

    asyncio.run(scenario())


def test_history_persist_then_cold_repository_recovers(tmp_path, monkeypatch):
    """首次请求打上游并异步落库；冷实例（模拟重启后内存为空）只读快照。"""
    db_path = tmp_path / "persist.db"
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    session_module._engine = None
    session_module._session_factory = None

    async def scenario() -> None:
        async with session_module.get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        today = datetime.now(UTC).date().isoformat()
        provider = FakeProvider(history=[{"date": today, "nav": 1.1, "dailyChange": 1.0}])
        repository = DataRepository(provider)
        await repository.ensure_funds()
        await _drain_persist(repository)

        records = await repository.history("000001", "1年")
        assert provider.history_calls == 1
        await _drain_history_persists(repository)

        async with session_module.get_session_factory()() as session:
            payload_gz = await session.scalar(select(RawDataSnapshot.payload_gz).where(
                RawDataSnapshot.endpoint == "fund-history:000001"))
            assert payload_gz is not None
            nav_rows = await session.scalar(
                select(func.count()).select_from(FundNavSnapshot))
            assert nav_rows == 1

        cold = DataRepository(FakeProvider())
        assert await cold.history("000001", "1年") == records
        assert cold.provider.history_calls == 0
        await session_module.get_engine().dispose()

    asyncio.run(scenario())
