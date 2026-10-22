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

    async def cleanup_snapshots() -> None:
        """清理过期快照，控住云端数据库的免费存储额度。

        教训（2026-10）：outbox 每 5 分钟轮询让 Neon 永不休眠，计算时长
        一个月打爆 190h 免费额度，数据库开始拒连，整个服务瘫掉。快照表
        同理只增不删 —— 每天全量目录 gzip + 补数新行，存储迟早重演。
        这里把 30 天前的原始/画像快照和已完成任务记录清掉；持仓披露
        周期长，单独放宽到 365 天。
        """
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text

        from ..db.session import get_session_factory

        now = datetime.now(UTC)
        cutoff_30 = now - timedelta(days=30)
        cutoff_365 = now - timedelta(days=365)
        statements = [
            ("raw_data_snapshots", "DELETE FROM raw_data_snapshots WHERE fetched_at < :c", cutoff_30),
            ("fund_profile_snapshots", "DELETE FROM fund_profile_snapshots WHERE fetched_at < :c", cutoff_30),
            ("fund_holding_snapshots", "DELETE FROM fund_holding_snapshots WHERE fetched_at < :c", cutoff_365),
            ("job_runs", "DELETE FROM job_runs WHERE started_at < :c", cutoff_30),
            ("outbox_events", "DELETE FROM outbox_events WHERE created_at < :c AND status != 'pending'", cutoff_30),
        ]
        async with get_session_factory()() as session:
            for name, sql, cutoff in statements:
                try:
                    result = await session.execute(text(sql), {"c": cutoff})
                    logger.info("cleanup %s deleted=%s", name, result.rowcount)
                except Exception as exc:  # noqa: BLE001 - 单表失败不阻塞其他表
                    logger.warning("cleanup %s failed: %s", name, str(exc)[:160])
            await session.commit()

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
    # outbox 没有真实消费者前，每天随批处理捎带消费一次即可。
    # 绝不能改成高频轮询：那会让 serverless 数据库永不休眠，
    # 计算时长配额一个月内必爆（2026-10 Neon 免费层实例教训）。
    scheduler.add_job(consume_outbox, "cron", hour=3, minute=30, id="outbox-consumer", replace_existing=True)
    scheduler.add_job(cleanup_snapshots, "cron", hour=3, minute=40, id="snapshot-cleanup", replace_existing=True)
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
