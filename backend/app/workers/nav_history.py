from __future__ import annotations

import logging

from ..db.nav_repository import NavSnapshotRepository
from ..models import Fund
from ..providers import AKShareProvider

logger = logging.getLogger(__name__)


async def sync_fund_history(fund: Fund, period: str = "1年") -> dict[str, object]:
    provider = AKShareProvider()
    records = await provider.history(fund.code, period)
    if not records:
        logger.error("fund history provider returned no records code=%s error=%s", fund.code, provider.status.last_error)
        return {"updated": 0, "status": "ERROR", "sourceStatus": provider.status.as_dict()}
    result = await NavSnapshotRepository().persist_history(
        fund, records, source_name=provider.status.source_name,
        source_type=provider.status.source_type, trust_level=provider.status.trust_level,
    )
    result["sourceStatus"] = provider.status.as_dict()
    logger.info("fund history snapshot persisted code=%s result=%s", fund.code, result)
    return result
