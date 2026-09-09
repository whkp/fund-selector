from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select

from ..models import Fund
from .base import FundNavSnapshot, FundRecord, RawDataSnapshot
from .session import get_session_factory


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class NavSnapshotRepository:
    async def persist_history(self, fund: Fund, records: list[dict[str, Any]], *, source_name: str,
                              source_type: str, trust_level: str,
                              parser_version: str = "akshare-history-v1") -> dict[str, Any]:
        if not records:
            return {"updated": 0, "snapshotId": None, "status": "EMPTY"}
        normalized = []
        for record in records:
            try:
                nav_date = date.fromisoformat(str(record["date"]))
                nav = float(record["nav"])
            except (KeyError, TypeError, ValueError):
                continue
            if nav <= 0:
                continue
            normalized.append({"date": nav_date.isoformat(), "nav": nav,
                               "dailyChange": float(record.get("dailyChange", 0) or 0)})
        if not normalized:
            return {"updated": 0, "snapshotId": None, "status": "INVALID"}
        normalized.sort(key=lambda item: item["date"])
        raw_text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        now = utcnow()
        raw_id = make_id("raw")
        async with get_session_factory()() as session:
            async with session.begin():
                existing = await session.scalar(select(RawDataSnapshot).where(
                    RawDataSnapshot.source_name == source_name,
                    RawDataSnapshot.endpoint == f"fund-history:{fund.code}",
                    RawDataSnapshot.content_hash == content_hash,
                    RawDataSnapshot.parser_version == parser_version,
                ))
                if existing is not None:
                    return {"updated": 0, "snapshotId": existing.id, "status": "UNCHANGED"}
                fund_record = await session.scalar(select(FundRecord).where(FundRecord.code == fund.code))
                if fund_record is None:
                    fund_record = FundRecord(
                        id=fund.id, code=fund.code, name=fund.name, short_name=fund.short_name,
                        created_at=now, updated_at=now,
                    )
                    session.add(fund_record)
                session.add(RawDataSnapshot(
                    id=raw_id, source_name=source_name, endpoint=f"fund-history:{fund.code}",
                    fetched_at=now, business_date=date.fromisoformat(normalized[-1]["date"]),
                    content_hash=content_hash, payload_text=raw_text,
                    parser_version=parser_version, http_status=200,
                ))
                for record in normalized:
                    session.add(FundNavSnapshot(
                        id=make_id("nav"), fund_id=fund_record.id, source_snapshot_id=raw_id,
                        nav_date=date.fromisoformat(record["date"]), unit_nav=record["nav"],
                        accumulated_nav=None, nav_type="official", source_type=source_type,
                        trust_level=trust_level, quality_status="REFERENCE",
                    ))
            return {"updated": len(normalized), "snapshotId": raw_id, "status": "COMPLETED"}
