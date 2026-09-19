from __future__ import annotations

import logging

from ..db.nav_repository import NavSnapshotRepository
from ..models import Fund
from ..providers import AKShareProvider

logger = logging.getLogger(__name__)


async def sync_fund_history(fund: Fund, period: str = "1年") -> dict[str, object]:
    """把单只基金的历史净值落成快照。

    ⚠️ **当前没有调用点** —— 全仓库只有这一处定义。历史 NAV 的批量同步还没实现
    （`docs/开发进展.md` 也这么写），这里是给生产化预留的骨架。将来接定时任务时
    把它接上，或者删掉，别让它一直悬在这里让人误以为在跑。
    """
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
