"""启动调度器但不等待触发任务，避免验证时访问公开数据源。"""
import asyncio

from app.workers.scheduler import build_scheduler


def test_scheduler_registers_and_starts_jobs():
    async def run():
        scheduler = build_scheduler()
        scheduler.start(paused=True)
        try:
            jobs = {job.id: job for job in scheduler.get_jobs()}
            assert set(jobs) == {"fund-universe", "fund-enrichment", "nav-prefill", "outbox-consumer", "snapshot-cleanup"}
            assert str(scheduler.timezone) == "Asia/Shanghai"
            assert all(job.next_run_time is not None for job in jobs.values())
            # 配额教训（2026-10 Neon 计算时长爆表）：所有任务必须是低频 cron，
            # 不允许再出现高频 interval 轮询让 serverless 数据库永不休眠。
            assert all(str(job.trigger).startswith("cron[") for job in jobs.values())
        finally:
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)

    asyncio.run(run())
