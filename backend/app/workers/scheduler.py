from __future__ import annotations

import asyncio
import logging

from app.providers import AKShareProvider, DataRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def run_once() -> bool:
    provider = AKShareProvider()
    repository = DataRepository(provider)
    updated = await repository.refresh_funds()
    logging.info("fund universe refresh finished updated=%s status=%s", updated, provider.status.status)
    return updated


if __name__ == "__main__":
    asyncio.run(run_once())
