from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select

from ..models import Fund
from .base import FundNavSnapshot, FundRecord, RawDataSnapshot
from .session import get_session_factory


def utcnow() -> datetime:
    return datetime.now(UTC)


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
                # 只取 id —— 不能 ORM 全实体读：命中的旧快照可能带着大块明文
                # payload_text（fund-universe 那套刚踩过：跨洋读回大字段会卡死）。
                existing_id = await session.scalar(select(RawDataSnapshot.id).where(
                    RawDataSnapshot.source_name == source_name,
                    RawDataSnapshot.endpoint == f"fund-history:{fund.code}",
                    RawDataSnapshot.content_hash == content_hash,
                    RawDataSnapshot.parser_version == parser_version,
                ))
                if existing_id is not None:
                    return {"updated": 0, "snapshotId": existing_id, "status": "UNCHANGED"}
                fund_record = await session.scalar(select(FundRecord).where(FundRecord.code == fund.code))
                if fund_record is None:
                    fund_record = FundRecord(
                        id=fund.id, code=fund.code, name=fund.name, short_name=fund.short_name,
                        created_at=now, updated_at=now,
                    )
                    session.add(fund_record)
                # 与 fund-universe 一致：只存 gzip 副本。NAV 明文单只不大，但按需
                # 落库会累积多只，跨洋读回时压缩副本快一个量级。
                session.add(RawDataSnapshot(
                    id=raw_id, source_name=source_name, endpoint=f"fund-history:{fund.code}",
                    fetched_at=now, business_date=date.fromisoformat(normalized[-1]["date"]),
                    content_hash=content_hash,
                    payload_gz=gzip.compress(raw_text.encode("utf-8")),
                    parser_version=parser_version, http_status=200,
                ))
                # 先把父行（funds / raw_data_snapshots）落库，再写引用它们的 nav 行。
                # 不要依赖 SQLAlchemy 的自动排序：那套顺序只在两个 mapper 之间有
                # relationship() 时才成立，这里只有裸 ForeignKey —— 实测它会把
                # 子表排在前面，一旦 PRAGMA foreign_keys=ON 就直接外键违约。
                await session.flush()
                for record in normalized:
                    session.add(FundNavSnapshot(
                        id=make_id("nav"), fund_id=fund_record.id, source_snapshot_id=raw_id,
                        nav_date=date.fromisoformat(record["date"]), unit_nav=record["nav"],
                        accumulated_nav=None, nav_type="official", source_type=source_type,
                        trust_level=trust_level, quality_status="REFERENCE",
                    ))
            return {"updated": len(normalized), "snapshotId": raw_id, "status": "COMPLETED"}
