"""自选（观察列表）的持久化。

以前自选是一个进程内 dict，所有访问者共用一份，重启即丢。多用户之后它必须
按 `user_id` 隔离并落库，否则任何拿到链接的人都能改别人的自选。

`watchlist_items.fund_id` 是指向 `funds.id` 的外键，而 `funds` 表只在
数据同步任务里才写入。用户加自选时基金可能还没同步进库，所以这里做一次
惰性 upsert，保证外键约束不会成为「加自选失败」的原因。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from .db.base import FundRecord, WatchlistItem
from .db.session import get_session_factory
from .models import Fund


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _ensure_fund_record(session: Any, fund: Fund) -> str:
    """返回可用于外键的 fund_id。"""
    existing = await session.scalar(select(FundRecord).where(FundRecord.id == fund.id))
    if existing is not None:
        return existing.id
    by_code = await session.scalar(select(FundRecord).where(FundRecord.code == fund.code))
    if by_code is not None:
        return by_code.id
    now = _utcnow()
    session.add(FundRecord(
        id=fund.id, code=fund.code, name=fund.name, short_name=fund.short_name,
        current_profile_snapshot_id=None, created_at=now, updated_at=now,
    ))
    await session.flush()
    return fund.id


def _item_public(fund: Fund, note: str, tags: list[str], added_at: datetime | None) -> dict[str, Any]:
    return {
        "fund": fund.as_dict(),
        "note": note or "",
        "reasonTags": tags or [],
        "addedAt": (added_at or _utcnow()).isoformat(),
    }


async def list_items(user_id: str, resolve: Any) -> list[dict[str, Any]]:
    """`resolve` 是一个 `code -> Fund | None` 的回调，用于补上实时行情字段。"""
    async with get_session_factory()() as session:
        # 注意：select 两个实体时只能用 execute()，scalars() 只支持单实体/单列。
        rows = await session.execute(
            select(WatchlistItem, FundRecord)
            .join(FundRecord, FundRecord.id == WatchlistItem.fund_id)
            .where(WatchlistItem.user_id == user_id)
            .order_by(WatchlistItem.created_at.desc())
        )
        items: list[dict[str, Any]] = []
        for item, record in rows.all():
            fund = resolve(record.code)
            if fund is None:
                # 基金已从目录中消失（下架/改名）时不伪造条目。
                continue
            items.append(_item_public(fund, item.note, list(item.reason_tags or []), item.created_at))
        return items


async def add_item(user_id: str, fund: Fund, note: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    now = _utcnow()
    async with get_session_factory()() as session:
        fund_id = await _ensure_fund_record(session, fund)
        existing = await session.scalar(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user_id, WatchlistItem.fund_id == fund_id
            )
        )
        if existing is not None:
            existing.note = note or existing.note
            existing.reason_tags = list(tags or existing.reason_tags or [])
            existing.updated_at = now
            await session.commit()
            return _item_public(fund, existing.note, list(existing.reason_tags or []), existing.created_at)
        record = WatchlistItem(
            id=f"wl_{uuid.uuid4().hex[:16]}",
            user_id=user_id,
            fund_id=fund_id,
            note=note or "",
            reason_tags=list(tags or []),
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        await session.commit()
        return _item_public(fund, record.note, list(record.reason_tags or []), record.created_at)


async def remove_item(user_id: str, code: str) -> bool:
    async with get_session_factory()() as session:
        fund = await session.scalar(select(FundRecord).where(FundRecord.code == code))
        if fund is None:
            return False
        result = await session.execute(
            delete(WatchlistItem).where(
                WatchlistItem.user_id == user_id, WatchlistItem.fund_id == fund.id
            )
        )
        await session.commit()
        return bool(result.rowcount)


async def contains(user_id: str, code: str) -> bool:
    async with get_session_factory()() as session:
        fund = await session.scalar(select(FundRecord).where(FundRecord.code == code))
        if fund is None:
            return False
        item = await session.scalar(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user_id, WatchlistItem.fund_id == fund.id
            )
        )
        return item is not None
