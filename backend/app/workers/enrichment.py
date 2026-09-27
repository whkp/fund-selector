"""精选基金批量补数任务。

任务是可中断、可重跑的：已有关键字段的基金会跳过，持仓快照按内容 hash
幂等写入；每只基金失败不会阻塞同批其他基金，JobRun.payload 持续记录进度。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ..db.base import JobRun, WatchlistItem
from ..db.session import get_session_factory
from ..enrichment import (
    _fetch_holdings,
    enrich_fund,
    load_holdings,
    needs_enrichment,
    persist_holdings,
)
from ..providers import AKShareProvider, DataRepository

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


async def _job_start(total: int) -> str:
    job_id = f"job_{uuid.uuid4().hex[:20]}"
    async with get_session_factory()() as session:
        session.add(JobRun(
            id=job_id, job_type="fund_enrichment", status="RUNNING", attempts=1,
            started_at=_now(), payload={"total": total, "completed": 0, "enriched": 0, "holdings": 0, "failed": 0},
        ))
        await session.commit()
    return job_id


async def _job_progress(job_id: str, payload: dict[str, Any], *, status: str | None = None) -> None:
    async with get_session_factory()() as session:
        job = await session.get(JobRun, job_id)
        if job is not None:
            job.payload = dict(payload)
            if status:
                job.status = status
                job.completed_at = _now()
            await session.commit()


async def select_targets(repository: DataRepository, limit: int | None = None) -> list[Any]:
    """合并浏览头部、主题头部和观察列表，按代码去重。"""
    ordered = repository.list_funds()
    selected: dict[str, Any] = {fund.code: fund for fund in ordered[:200]}
    by_theme: dict[str, list[Any]] = {}
    for fund in ordered:
        theme = str(fund.theme or "").strip()
        if theme and theme not in {"未获取", "未标注", "未知"}:
            by_theme.setdefault(theme, []).append(fund)
    for items in by_theme.values():
        for fund in items[:50]:
            selected[fund.code] = fund
    # 观察列表是用户明确表达过兴趣的集合，即使它不在浏览前 200 也优先补齐。
    try:
        async with get_session_factory()() as session:
            codes = (await session.scalars(select(WatchlistItem.fund_id))).all()
        for fund_id in codes:
            fund = next((item for item in ordered if item.id == fund_id), None)
            if fund is not None:
                selected[fund.code] = fund
    except Exception as exc:  # noqa: BLE001 - 数据库不可用时仍可补浏览集合
        logger.warning("读取观察列表补数目标失败：%s", exc)
    result = list(selected.values())
    if limit is not None:
        result = result[:max(0, limit)]
    return result


async def run_batch(repository: DataRepository, *, limit: int | None = None,
                    concurrency: int = 3, retries: int = 3) -> dict[str, Any]:
    targets = await select_targets(repository, limit)
    job_id = await _job_start(len(targets))
    semaphore = asyncio.Semaphore(max(1, concurrency))
    progress: dict[str, Any] = {"total": len(targets), "completed": 0, "enriched": 0, "holdings": 0, "failed": 0}
    progress_lock = asyncio.Lock()

    async def one(fund: Any) -> None:
        async with semaphore:
            try:
                if needs_enrichment(fund):
                    for attempt in range(max(1, retries)):
                        try:
                            result = await enrich_fund(repository, fund.code)
                            if result is None:
                                raise RuntimeError("enrichment returned no result")
                            async with progress_lock:
                                if result.get("updated"):
                                    progress["enriched"] += 1
                            break
                        except Exception:  # noqa: BLE001 - 单只失败允许重试
                            if attempt + 1 >= max(1, retries):
                                raise
                            await asyncio.sleep(0.25 * (2 ** attempt))
                if not await load_holdings(fund.code):
                    for attempt in range(max(1, retries)):
                        try:
                            holdings = await asyncio.to_thread(_fetch_holdings, fund.code)
                            result = await persist_holdings(fund.code, holdings)
                            if result.get("updated"):
                                async with progress_lock:
                                    progress["holdings"] += 1
                            break
                        except Exception:
                            if attempt + 1 >= max(1, retries):
                                raise
                            await asyncio.sleep(0.25 * (2 ** attempt))
            except Exception as exc:  # noqa: BLE001 - 记录后继续其他基金
                logger.warning("基金 %s 批量补数失败：%s", fund.code, str(exc)[:200])
                async with progress_lock:
                    progress["failed"] += 1
            finally:
                async with progress_lock:
                    progress["completed"] += 1
                    # 进度写入也串行，避免较旧的完成计数晚提交后覆盖新值。
                    await _job_progress(job_id, dict(progress))

    await asyncio.gather(*(one(fund) for fund in targets))
    status = "PARTIAL" if progress["failed"] else "COMPLETED"
    await _job_progress(job_id, progress, status=status)
    return {"jobId": job_id, **progress, "status": status}


async def run_once(*, limit: int | None = None, concurrency: int = 3, retries: int = 3,
                   repository: DataRepository | None = None) -> dict[str, Any]:
    repo = repository or DataRepository(AKShareProvider())
    if repository is None:
        from ..db.snapshot_repository import load_fund_universe

        loaded = await load_fund_universe()
        if loaded:
            await repo.adopt_universe(*loaded)
        else:
            await repo.refresh_funds()
    return await run_batch(repo, limit=limit, concurrency=concurrency, retries=retries)


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐精选基金主数据与前十大持仓")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少只基金")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = asyncio.run(run_once(limit=args.limit, concurrency=args.concurrency, retries=args.retries))
    logger.info("enrichment finished: %s", result)


if __name__ == "__main__":
    main()
