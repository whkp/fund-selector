from __future__ import annotations

import asyncio
import logging

from app.workers.fund_universe import sync_fund_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 用模块级 logger 而不是 root logger：root 上的记录会被 basicConfig 之外的
# 任何 handler 重复处理，而且没法按模块过滤。
logger = logging.getLogger(__name__)


async def run_once() -> bool:
    """跑一次基金目录同步。

    ⚠️ 名字叫 scheduler，但**只执行一次就退出** —— 没有循环，也没有定时。
    真正的周期调度尚未实现，`package.json` 里的 `npm run worker` 同样是单次执行。
    要么在这里接上 APScheduler 或系统计划任务，要么就别假设它会自己跑起来。
    """
    result = await sync_fund_universe()
    logger.info("fund universe refresh finished result=%s", result)
    return bool(result.get("updated"))


if __name__ == "__main__":
    asyncio.run(run_once())
