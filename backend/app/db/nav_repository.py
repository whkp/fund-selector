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
    async def load_latest_history(self, code: str) -> tuple[list[dict[str, Any]], date] | None:
        """取该基金最近一次落库的历史净值快照（有 gzip 副本就优先用它）。

        两段式查询：先只取定位列（id / 是否 gz / business_date），再按需读
        payload 列 —— 直接 ORM 全实体读会把大字段整行拉回，跨洋库上这笔
        传输纯属浪费（fund-universe 那套已经因此卡死过）。返回
        (records, business_date)；没有可用快照时返回 None。
        """
        async with get_session_factory()() as session:
            has_gz = RawDataSnapshot.payload_gz.is_not(None)
            row = (await session.execute(
                select(RawDataSnapshot.id, has_gz.label("has_gz"), RawDataSnapshot.business_date)
                .where(RawDataSnapshot.endpoint == f"fund-history:{code}")
                .order_by(has_gz.desc(), RawDataSnapshot.fetched_at.desc())
                .limit(1)
            )).first()
            if row is None:
                return None
            if row.has_gz:
                payload_bytes = await session.scalar(
                    select(RawDataSnapshot.payload_gz).where(RawDataSnapshot.id == row.id))
                if payload_bytes is None:
                    return None
                raw_text = gzip.decompress(payload_bytes).decode("utf-8")
            else:
                raw_text = await session.scalar(
                    select(RawDataSnapshot.payload_text).where(RawDataSnapshot.id == row.id))
                if raw_text is None:
                    return None
        try:
            records = json.loads(raw_text)
        except (TypeError, ValueError):
            return None
        if not isinstance(records, list) or not records:
            return None
        business_date = row.business_date
        if business_date is None:
            # 更早版本可能没写 business_date：退回记录里的最后一天（写入侧已排过序）。
            try:
                business_date = date.fromisoformat(str(records[-1]["date"]))
            except (KeyError, TypeError, ValueError):
                return None
        return records, business_date

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
