"""DataRepository 的 TTL 行为。

回归背景：ensure_funds() 曾经写作 `bool(self.funds) or await self.refresh_funds()`，
只要内存非空就永久返回首个快照，导致进程存活期间数据完全冻结。这些测试锁定
「缓存新鲜时不打上游、过期后必须重新拉取、上游失败时降级为旧快照并推迟重试」
三条不变量。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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

    async def history(self, code, period):  # noqa: ARG002
        self.history_calls += 1
        return [] if self.fail else self.history_records


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
        repository._funds_fetched_at = datetime.now(timezone.utc) - timedelta(seconds=3601)
        assert await repository.ensure_funds() is True
        assert provider.list_calls == 2

    asyncio.run(scenario())


def test_ensure_funds_serves_stale_snapshot_when_upstream_fails():
    async def scenario() -> None:
        provider = FakeProvider()
        repository = DataRepository(provider)
        await repository.ensure_funds()
        assert provider.list_calls == 1

        provider.fail = True
        repository._funds_fetched_at = datetime.now(timezone.utc) - timedelta(seconds=100_000)
        assert await repository.ensure_funds() is True
        assert provider.list_calls == 2
        # 上游失败时旧快照仍可服务。
        assert len(repository.list_funds()) == 1
        # 失败后推迟重试，不应每个请求都打上游。
        assert await repository.ensure_funds() is True
        assert provider.list_calls == 2

    asyncio.run(scenario())


def test_history_refetches_after_ttl_expires():
    async def scenario() -> None:
        provider = FakeProvider()
        repository = DataRepository(provider)
        repository.history_ttl_seconds = 3600

        await repository.history("000001", "1年")
        await repository.history("000001", "1年")
        assert provider.history_calls == 1

        repository._history_fetched_at[("000001", "1年")] = (
            datetime.now(timezone.utc) - timedelta(seconds=3601)
        )
        await repository.history("000001", "1年")
        assert provider.history_calls == 2

    asyncio.run(scenario())


def test_funds_refresh_clears_history_cache():
    async def scenario() -> None:
        provider = FakeProvider()
        repository = DataRepository(provider)

        await repository.ensure_funds()
        await repository.history("000001", "1年")
        assert ("000001", "1年") in repository.histories

        await repository.refresh_funds()
        assert repository.histories == {}
        assert repository._history_fetched_at == {}

    asyncio.run(scenario())
