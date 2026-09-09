from __future__ import annotations

import asyncio
import logging

from app.workers.fund_universe import sync_fund_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def run_once() -> bool:
    result = await sync_fund_universe()
    logging.info("fund universe refresh finished result=%s", result)
    return bool(result.get("updated"))


if __name__ == "__main__":
    asyncio.run(run_once())
