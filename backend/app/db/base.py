from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FundRecord(TimestampMixin, Base):
    __tablename__ = "funds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(100))
    current_profile_snapshot_id: Mapped[str | None] = mapped_column(String(64))


class RawDataSnapshot(Base):
    __tablename__ = "raw_data_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    business_date: Mapped[date | None] = mapped_column(Date)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_uri: Mapped[str | None] = mapped_column(String(1000))
    payload_text: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))


class FundProfileSnapshot(Base):
    __tablename__ = "fund_profile_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fund_id: Mapped[str] = mapped_column(ForeignKey("funds.id"), index=True, nullable=False)
    raw_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("raw_data_snapshots.id"))
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    company: Mapped[str | None] = mapped_column(String(300))
    manager: Mapped[str | None] = mapped_column(String(200))
    manager_years: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    risk_level: Mapped[str | None] = mapped_column(String(40))
    fee_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    scale: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    inception_date: Mapped[date | None] = mapped_column(Date)
    subscription_status: Mapped[str | None] = mapped_column(String(60))
    theme: Mapped[str | None] = mapped_column(String(300))
    business_date: Mapped[date | None] = mapped_column(Date, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(30), nullable=False)
    quality_status: Mapped[str] = mapped_column(String(30), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)


class FundNavSnapshot(Base):
    __tablename__ = "fund_nav_snapshots"
    __table_args__ = (UniqueConstraint("fund_id", "nav_date", "nav_type", "source_snapshot_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fund_id: Mapped[str] = mapped_column(ForeignKey("funds.id"), index=True, nullable=False)
    source_snapshot_id: Mapped[str] = mapped_column(ForeignKey("raw_data_snapshots.id"), nullable=False)
    nav_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    unit_nav: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    accumulated_nav: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    nav_type: Mapped[str] = mapped_column(String(30), nullable=False, default="official")
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(30), nullable=False)
    quality_status: Mapped[str] = mapped_column(String(30), nullable=False)


class FundQuoteSnapshot(Base):
    __tablename__ = "fund_quote_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fund_id: Mapped[str] = mapped_column(ForeignKey("funds.id"), index=True, nullable=False)
    source_snapshot_id: Mapped[str] = mapped_column(ForeignKey("raw_data_snapshots.id"), nullable=False)
    quote_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    previous_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    iopv: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    venue: Mapped[str] = mapped_column(String(30), nullable=False)
    freshness_status: Mapped[str] = mapped_column(String(30), nullable=False)


class FundMetricSnapshot(Base):
    __tablename__ = "fund_metric_snapshots"
    __table_args__ = (UniqueConstraint("fund_id", "input_snapshot_id", "window_code", "metric_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fund_id: Mapped[str] = mapped_column(ForeignKey("funds.id"), index=True, nullable=False)
    input_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    window_code: Mapped[str] = mapped_column(String(30), nullable=False)
    metric_version: Mapped[str] = mapped_column(String(80), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    business_date: Mapped[date | None] = mapped_column(Date, index=True)
    total_return: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    annual_return: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    volatility: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    drawdown: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    calmar: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    insufficiency_reason: Mapped[str | None] = mapped_column(String(500))
    risk_free_rate_version: Mapped[str | None] = mapped_column(String(80))


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), index=True, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskProfile(Base):
    __tablename__ = "risk_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    questionnaire_version: Mapped[str] = mapped_column(String(80), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(40), nullable=False)
    investment_horizon_months: Mapped[int | None] = mapped_column(Integer)
    liquidity_need: Mapped[str | None] = mapped_column(String(40))
    goal_type: Mapped[str | None] = mapped_column(String(100))
    answers: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "fund_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    fund_id: Mapped[str] = mapped_column(ForeignKey("funds.id"), index=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecommendationRun(Base):
    __tablename__ = "recommendation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    data_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    metric_version: Mapped[str | None] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(160))
    request_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    interpretation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecommendationStep(Base):
    __tablename__ = "recommendation_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("recommendation_runs.id"), index=True, nullable=False)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(128))
    output_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
