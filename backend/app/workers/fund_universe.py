from __future__ import annotations

import logging

from ..db.snapshot_repository import SnapshotRepository
from ..providers import AKShareProvider

logger = logging.getLogger(__name__)


async def sync_fund_universe() -> dict[str, object]:
    provider = AKShareProvider()
    funds = await provider.list_funds()
    if not funds:
        logger.error("fund universe provider returned no records: %s", provider.status.last_error)
        return {"updated": 0, "status": "ERROR", "sourceStatus": provider.status.as_dict()}
    result = await SnapshotRepository().persist_fund_universe(
        funds, source_name=provider.status.source_name,
        source_type=provider.status.source_type, trust_level=provider.status.trust_level,
    )
    result["sourceStatus"] = provider.status.as_dict()
    logger.info("fund universe snapshot persisted: %s", result)
    return result
