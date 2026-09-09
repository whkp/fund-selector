from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..models import Fund
from .base import FundProfileSnapshot, FundRecord, JobRun, OutboxEvent, RawDataSnapshot
from .session import get_session_factory


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def fund_payload(fund: Fund) -> dict[str, Any]:
    return fund.as_dict()


class SnapshotRepository:
    """Persistence boundary for immutable provider snapshots.

    The current HTTP repository remains memory-backed during migration. This
    repository is used by workers first, so a failed sync never replaces the
    last successful in-memory/API dataset.
    """

    async def persist_fund_universe(self, funds: list[Fund], *, source_name: str,
                                    source_type: str, trust_level: str,
                                    parser_version: str = "akshare-rank-v1") -> dict[str, Any]:
        if not funds:
            return {"updated": 0, "snapshotId": None, "status": "EMPTY"}
        now = utcnow()
        payload = [fund_payload(fund) for fund in funds]
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        raw_id = make_id("raw")
        job_id = make_id("job")
        async with get_session_factory()() as session:
            async with session.begin():
                existing = await session.scalar(select(RawDataSnapshot).where(
                    RawDataSnapshot.source_name == source_name,
                    RawDataSnapshot.content_hash == content_hash,
                    RawDataSnapshot.parser_version == parser_version,
                ))
                if existing is not None:
                    return {"updated": 0, "snapshotId": existing.id, "status": "UNCHANGED"}
                session.add(JobRun(id=job_id, job_type="fund_universe_sync", status="RUNNING",
                                   attempts=1, started_at=now, payload={"count": len(funds)}))
                session.add(RawDataSnapshot(
                    id=raw_id, source_name=source_name, endpoint="fund-universe",
                    fetched_at=now, business_date=now.date(), content_hash=content_hash,
                    payload_text=raw_text, parser_version=parser_version, http_status=200,
                ))
                written = 0
                for fund in funds:
                    record = await session.scalar(select(FundRecord).where(FundRecord.code == fund.code))
                    if record is None:
                        record = FundRecord(id=fund.id, code=fund.code, name=fund.name,
                                            short_name=fund.short_name, created_at=now, updated_at=now)
                        session.add(record)
                    else:
                        record.name = fund.name
                        record.short_name = fund.short_name
                        record.updated_at = now
                    profile_id = make_id("profile")
                    session.add(FundProfileSnapshot(
                        id=profile_id, fund_id=record.id, raw_snapshot_id=raw_id,
                        provider=source_name, company=fund.company, manager=fund.manager,
                        manager_years=fund.manager_years, risk_level=fund.risk,
                        fee_rate=fund.fee, scale=fund.scale, subscription_status=fund.intake,
                        theme=fund.theme, business_date=now.date(), fetched_at=now,
                        source_type=source_type, trust_level=trust_level,
                        quality_status=fund.quality_status, parser_version=parser_version,
                    ))
                    record.current_profile_snapshot_id = profile_id
                    written += 1
                session.add(OutboxEvent(
                    id=make_id("event"), event_type="fund_universe.synced",
                    aggregate_id=raw_id, payload={"rawSnapshotId": raw_id, "fundCount": written},
                    status="pending", attempts=0, created_at=now,
                ))
                job = await session.scalar(select(JobRun).where(JobRun.id == job_id))
                if job is not None:
                    job.status = "COMPLETED"
                    job.completed_at = now
            return {"updated": written, "snapshotId": raw_id, "jobId": job_id, "status": "COMPLETED"}
