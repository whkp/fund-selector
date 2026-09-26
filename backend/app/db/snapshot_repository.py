from __future__ import annotations

import gzip
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from ..models import Fund
from .base import FundProfileSnapshot, FundRecord, JobRun, OutboxEvent, RawDataSnapshot
from .session import get_session_factory

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def fund_payload(fund: Fund) -> dict[str, Any]:
    return fund.as_dict()


# Fund.as_dict() 的 camelCase 键 ↔ dataclass 字段名映射。启动时靠它把快照
# payload 还原成 Fund；改动 Fund 字段时必须两边同步，否则「从数据库装载」
# 会静默丢字段。
_FUND_PAYLOAD_FIELDS = {
    "id": "id", "code": "code", "name": "name", "shortName": "short_name",
    "type": "type", "risk": "risk", "manager": "manager",
    "managerYears": "manager_years", "company": "company", "theme": "theme",
    "nav": "nav", "accumulatedNav": "accumulated_nav", "navDate": "nav_date",
    "navSourceType": "nav_source_type", "navTrustLevel": "nav_trust_level",
    "navFreshness": "nav_freshness", "ytd": "ytd", "oneYear": "one_year",
    "volatility": "volatility", "drawdown": "drawdown", "fee": "fee",
    "scale": "scale", "inception": "inception", "score": "score",
    "scoreParts": "score_parts", "reason": "reason", "caveat": "caveat",
    "highlights": "highlights", "status": "status", "source": "source",
    "snapshot": "snapshot", "chart": "chart", "tags": "tags",
    "intake": "intake", "qualityStatus": "quality_status",
}


def fund_from_payload(payload: dict[str, Any]) -> Fund:
    """把 fund_payload() 产出的字典还原成 Fund（未知键丢弃，缺失键置 None）。"""
    return Fund(**{field: payload.get(key) for key, field in _FUND_PAYLOAD_FIELDS.items()})


async def load_fund_universe() -> tuple[list[Fund], datetime] | None:
    """从最近一次 fund-universe 原始快照重建基金目录（启动装载用）。

    优先读带 gzip 压缩副本的快照（新格式，~600 KB）；旧库只有明文快照时
    回退读它（~15 MB，慢但可用）。返回 (funds, fetched_at)；没有可用快照
    或内容损坏时返回 None，调用方回落到实时抓取。用标准 SQL（不依赖
    PostgreSQL 专有语法）以便测试。
    """
    async with get_session_factory()() as session:
        snapshot = await session.scalar(
            select(RawDataSnapshot)
            .where(RawDataSnapshot.endpoint == "fund-universe")
            .order_by(
                # 有压缩副本的优先（boolean 排序：True 在前），再按时间取最新。
                RawDataSnapshot.payload_gz.is_not(None).desc(),
                RawDataSnapshot.fetched_at.desc(),
            )
            .limit(1),
        )
    if snapshot is None:
        return None
    if snapshot.payload_gz is not None:
        try:
            raw_text = gzip.decompress(snapshot.payload_gz).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            logger.warning("目录快照 %s 解压失败，忽略", snapshot.id)
            return None
    elif snapshot.payload_text:
        raw_text = snapshot.payload_text
    else:
        return None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("目录快照 %s 解析失败，忽略", snapshot.id)
        return None
    if not isinstance(payload, list):
        return None
    funds = [fund_from_payload(item) for item in payload if isinstance(item, dict)]
    if not funds:
        return None
    return funds, snapshot.fetched_at


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
        # default=str 让 date 这类非 JSON 原生类型以字符串落盘 —— 快照的首要
        # 职责是「塞得进、读得回」，一只基金的未预期类型不该让整次落库崩掉。
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        # 明文 ~15 MB 在跨洋链路上读回要十几分钟；gzip 后 ~600 KB，装载路径
        # 只读压缩副本。hash 仍按明文算，两种格式的内容判定保持一致。
        payload_gz = gzip.compress(raw_text.encode("utf-8"))
        raw_id = make_id("raw")
        job_id = make_id("job")
        async with get_session_factory()() as session:
            async with session.begin():
                # 只取判定需要的列 —— 不能 ORM 全实体读：命中的旧格式快照带着
                # 15 MB 的明文 payload_text，跨洋读一次要十几分钟（worker 实测卡死）。
                existing = (await session.execute(select(
                    RawDataSnapshot.id,
                    RawDataSnapshot.payload_gz.is_(None).label("needs_gzip"),
                ).where(
                    RawDataSnapshot.source_name == source_name,
                    RawDataSnapshot.content_hash == content_hash,
                    RawDataSnapshot.parser_version == parser_version,
                ))).first()
                if existing is not None:
                    existing_id, needs_gzip = existing
                    if needs_gzip:
                        # 旧格式快照（只有明文）：内容没变，就地补压缩副本并清掉
                        # 明文，让装载路径能走快通道。用 SQL UPDATE 而不是 ORM
                        # 取回实体 —— 后者会把它那 15 MB 明文又拉一遍。
                        await session.execute(
                            update(RawDataSnapshot)
                            .where(RawDataSnapshot.id == existing_id)
                            .values(payload_gz=payload_gz, payload_text=None),
                            execution_options={"synchronize_session": False},
                        )
                        return {"updated": 0, "snapshotId": existing_id, "status": "COMPRESSED"}
                    return {"updated": 0, "snapshotId": existing_id, "status": "UNCHANGED"}
                session.add(JobRun(id=job_id, job_type="fund_universe_sync", status="RUNNING",
                                   attempts=1, started_at=now, payload={"count": len(funds)}))
                session.add(RawDataSnapshot(
                    id=raw_id, source_name=source_name, endpoint="fund-universe",
                    fetched_at=now, business_date=now.date(), content_hash=content_hash,
                    payload_gz=payload_gz, parser_version=parser_version, http_status=200,
                ))
                written = 0
                staged: list[tuple[FundRecord, str, Fund]] = []
                # 全量 universe 约 2 万行，循环里逐行 SELECT 就是 2 万次网络往返，
                # 对着 Neon 这种跨洋库冷启动要按小时算。一次把 (code -> 行) 全拿回来。
                existing_by_code: dict[str, FundRecord] = {
                    row.code: row for row in (await session.scalars(select(FundRecord))).all()
                }
                for fund in funds:
                    record = existing_by_code.get(fund.code)
                    if record is None:
                        record = FundRecord(id=fund.id, code=fund.code, name=fund.name,
                                            short_name=fund.short_name, created_at=now, updated_at=now)
                        session.add(record)
                    else:
                        record.name = fund.name
                        record.short_name = fund.short_name
                        record.updated_at = now
                    staged.append((record, make_id("profile"), fund))
                    written += 1
                # 先把 funds 落库，再写引用它们的 profile 快照。理由同 nav_repository：
                # 裸 ForeignKey 不参与 SQLAlchemy 的 INSERT 排序，父行必须显式先 flush ——
                # 否则下一轮循环里的 SELECT 触发 autoflush 时会先插子表，开启
                # PRAGMA foreign_keys 之后直接外键违约。
                await session.flush()
                for record, profile_id, fund in staged:
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
