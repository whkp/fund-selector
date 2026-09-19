from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Fund:
    id: str
    code: str
    name: str
    short_name: str
    type: str
    risk: str
    manager: str
    manager_years: float | None
    company: str
    theme: str
    nav: float | None
    nav_date: str
    ytd: float | None
    one_year: float | None
    volatility: float | None
    drawdown: float | None
    fee: float | None
    scale: float | None
    inception: float | None
    score: int
    score_parts: list[dict[str, Any]]
    reason: str
    caveat: str
    highlights: list[str]
    status: str
    source: str
    snapshot: str
    chart: list[float]
    tags: list[str]
    intake: str
    quality_status: str
    accumulated_nav: float | None = None
    nav_source_type: str = "AKSHARE_PUBLIC"
    nav_trust_level: str = "LOW"
    nav_freshness: str = "REFERENCE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "code": self.code, "name": self.name, "shortName": self.short_name,
            "type": self.type, "risk": self.risk, "manager": self.manager,
            "managerYears": self.manager_years, "company": self.company, "theme": self.theme,
            "nav": self.nav, "accumulatedNav": self.accumulated_nav, "navDate": self.nav_date,
            "navSourceType": self.nav_source_type, "navTrustLevel": self.nav_trust_level,
            "navFreshness": self.nav_freshness, "ytd": self.ytd, "oneYear": self.one_year,
            "volatility": self.volatility, "drawdown": self.drawdown, "fee": self.fee,
            "scale": self.scale, "inception": self.inception, "score": self.score,
            "scoreParts": self.score_parts, "reason": self.reason, "caveat": self.caveat,
            "highlights": self.highlights, "status": self.status, "source": self.source,
            "snapshot": self.snapshot, "chart": self.chart, "tags": self.tags,
            "intake": self.intake, "qualityStatus": self.quality_status,
        }


@dataclass
class WatchItem:
    fund: Fund
    note: str = ""
    reason_tags: list[str] = field(default_factory=list)
    added_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {"fund": self.fund.as_dict(), "note": self.note, "reasonTags": self.reason_tags,
                "addedAt": self.added_at.isoformat()}


@dataclass
class SourceStatus:
    source_name: str
    source_type: str
    trust_level: str
    status: str
    configured: bool
    license_scope: str
    field_coverage: list[str]
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        result = {
            "sourceName": self.source_name, "sourceType": self.source_type,
            "trustLevel": self.trust_level, "status": self.status, "configured": self.configured,
            "licenseScope": self.license_scope, "fieldCoverage": self.field_coverage,
        }
        if self.last_attempt_at:
            result["lastAttemptAt"] = self.last_attempt_at.isoformat()
        if self.last_success_at:
            result["lastSuccessAt"] = self.last_success_at.isoformat()
        if self.last_error:
            result["lastError"] = self.last_error
        return result
