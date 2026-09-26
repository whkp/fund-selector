from __future__ import annotations

import logging

from ..db.nav_repository import NavSnapshotRepository
from ..models import Fund
from ..providers import AKShareProvider

logger = logging.getLogger(__name__)


async def sync_fund_history(fund: Fund, period: str = "1年") -> dict[str, object]:
    """把单只基金的历史净值落成快照（批量预填充骨架）。

    按需落库已由 `DataRepository.history()` 实现：请求 1年 窗口且内存未命中时会
    先查数据库快照回收，拉取成功后异步落库（见 providers.py 的
    _recover_history / _schedule_history_persist）。这里保留给将来的批量预填充
    （定时任务把热门/关注基金先写满），当前没有调用点。
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
