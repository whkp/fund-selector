"""最小 outbox 消费者：先把待处理事件写入审计日志并标记完成。

现在没有外部通知消费者，仍要避免 pending 无限堆积；未来接消息队列时只需
替换 ``handle_event``，数据库事务和重试字段保持不变。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from ..db.base import OutboxEvent
from ..db.session import get_session_factory

logger = logging.getLogger(__name__)


async def consume_once(limit: int = 100) -> int:
    processed = 0
    async with get_session_factory()() as session:
        events = (await session.scalars(
            select(OutboxEvent).where(OutboxEvent.status == "pending")
            .order_by(OutboxEvent.created_at).limit(max(1, limit))
        )).all()
        for event in events:
            logger.info("outbox event processed type=%s aggregate=%s", event.event_type, event.aggregate_id)
            event.status = "completed"
            event.attempts = int(event.attempts or 0) + 1
            event.processed_at = datetime.now(UTC)
            processed += 1
        await session.commit()
    return processed
