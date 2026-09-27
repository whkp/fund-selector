from __future__ import annotations

import asyncio
import logging

from app.workers.fund_universe import sync_fund_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 用模块级 logger 而不是 root logger：root 上的记录会被 basicConfig 之外的
# 任何 handler 重复处理，而且没法按模块过滤。
logger = logging.getLogger(__name__)


def build_scheduler():
    """构造每日 02:00 的增量任务调度器。"""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError as exc:  # pragma: no cover - 依赖缺失时由部署检查暴露
        raise RuntimeError("请安装 APScheduler 才能运行周期调度") from exc
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def refresh_universe() -> None:
        await sync_fund_universe()

    async def enrich_selected() -> None:
        from app.workers.enrichment import run_once

        await run_once()

    async def consume_outbox() -> None:
        from app.workers.outbox import consume_once

        await consume_once()

    async def prefill_history() -> None:
        """预填充评分靠前的热门基金 1 年 NAV，复用 Repository 的 gzip 落库路径。"""
        from app.main import repository

        await repository.ensure_funds()
        # 只预填充头部 50 只，避免凌晨任务把公开接口打成全量抓取。
        for fund in repository.list_funds()[:50]:
            try:
                await repository.history(fund.code, "1年")
            except Exception as exc:  # noqa: BLE001 - 单只失败不影响其他基金
                logger.warning("热门历史 NAV 预填充失败 code=%s error=%s", fund.code, str(exc)[:160])

    scheduler.add_job(refresh_universe, "cron", hour=2, minute=0, id="fund-universe", replace_existing=True)
    scheduler.add_job(enrich_selected, "cron", hour=2, minute=20, id="fund-enrichment", replace_existing=True)
    scheduler.add_job(prefill_history, "cron", hour=3, minute=0, id="nav-prefill", replace_existing=True)
    scheduler.add_job(consume_outbox, "interval", minutes=5, id="outbox-consumer", replace_existing=True)
    return scheduler


async def run_forever() -> None:
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("周期调度已启动：每日 02:00 目录刷新，02:20 精选基金补数")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


async def run_once() -> bool:
    """兼容旧入口：仅执行一次基金目录同步。周期模式使用 ``run_forever``。"""
    result = await sync_fund_universe()
    logger.info("fund universe refresh finished result=%s", result)
    return bool(result.get("updated"))


if __name__ == "__main__":
    asyncio.run(run_forever())
