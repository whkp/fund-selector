"""批量补数与持仓快照的离线回归测试。"""
from __future__ import annotations

import asyncio

import pytest

from app import enrichment
from app.db import session as session_module
from app.db.base import Base, JobRun
from app.providers import DataRepository
from app.workers import enrichment as worker
from tests.test_scoring import make_fund


def test_holding_overlap_only_warns_at_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = {
        "A": [{"code": str(index), "name": f"股票{index}", "weight": 1.0} for index in range(6)],
        "B": [{"code": str(index), "name": f"股票{index}", "weight": 1.0} for index in range(5)]
        + [{"code": "other", "name": "其他", "weight": 1.0}],
        "C": [{"code": "only-one", "name": "单只", "weight": 1.0}],
    }

    async def fake_load(code: str):
        return snapshots.get(code, [])

    monkeypatch.setattr(enrichment, "load_holdings", fake_load)
    warnings = asyncio.run(enrichment.holding_overlap(["A", "B", "C"]))
    assert len(warnings) == 1
    assert warnings[0]["fundCodes"] == ["A", "B"]
    assert warnings[0]["overlapCount"] == 5


def test_persist_holdings_is_idempotent() -> None:
    holdings = [{"code": "600000", "name": "浦发银行", "weight": 2.1}]
    first = asyncio.run(enrichment.persist_holdings("TEST-HOLD", holdings))
    second = asyncio.run(enrichment.persist_holdings("TEST-HOLD", holdings))
    assert first["status"] == "COMPLETED"
    assert second["status"] == "UNCHANGED"
    assert asyncio.run(enrichment.load_holdings("TEST-HOLD")) == holdings


@pytest.mark.parametrize("always_fail", [False, True])
def test_worker_retries_records_progress_and_skips_completed(tmp_path, monkeypatch, always_fail):
    monkeypatch.setenv("FUND_COMPASS_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'worker.db').as_posix()}")
    calls = 0
    holding_calls = 0

    class FakeProvider:
        async def list_funds(self):
            raise AssertionError("测试不能调用上游目录")

    repo = DataRepository(FakeProvider())
    fund = make_fund()
    repo.funds = {fund.code: fund}

    async def fake_enrich(repository, code):
        nonlocal calls
        calls += 1
        if always_fail or calls == 1:
            return None
        fund.manager = "测试经理"
        fund.company = "测试公司"
        fund.risk = "中风险"
        fund.scale = 10
        fund.drawdown = -5
        return {"code": code, "updated": {"risk": fund.risk}}

    def fake_holdings(code):
        nonlocal holding_calls
        holding_calls += 1
        return [{"code": "600000", "name": "测试股票", "weight": 2.1}]

    monkeypatch.setattr(worker, "enrich_fund", fake_enrich)
    monkeypatch.setattr(worker, "_fetch_holdings", fake_holdings)

    async def run():
        engine = session_module.get_engine()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            result = await worker.run_batch(repo, retries=2)
            assert calls == 2
            assert result["completed"] == 1
            assert result["failed"] == int(always_fail)
            assert result["status"] == ("PARTIAL" if always_fail else "COMPLETED")
            async with session_module.get_session_factory()() as session:
                job = await session.get(JobRun, result["jobId"])
                assert job.status == result["status"]
                assert job.payload["completed"] == 1
            if not always_fail:
                second = await worker.run_batch(repo)
                assert second["enriched"] == second["holdings"] == 0
                assert calls == 2
                assert holding_calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
