"""按需补齐单只基金缺失的主数据与指标（enrichment）。

背景：AKShare 排行接口只产出 name/type/nav/收益/fee（实测 20322 只全量），
manager/company/scale/inception/theme 全是占位值，drawdown/volatility 全空。

数据源（AKShare 雪球逐只接口，实测 161725 通过）：
- fund_individual_basic_info_xq  → 基金公司/经理/成立时间/最新规模/业绩比较基准
- fund_individual_achievement_xq → 官方口径各区间最大回撤（近1年等，含同类排名）

策略：
1. 按需：agent 的 get_fund_detail 命中占位数据时才逐只拉取（每轮研究有预算上限，
   避免一次研究打爆上游）；
2. 即效：内存 Fund 直接改字段，并重建 n-gram 索引让新经理/主题立刻可被检索；
3. 持久：Neon 落 fund_profile_snapshots + fund_metric_snapshots，
   启动时 load_enriched() 回填 —— 同一只基金只补一次，重启不丢。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, date, datetime
from typing import Any

from .models import Fund
from .providers import build_gram_index

logger = logging.getLogger(__name__)

PLACEHOLDERS = {"未获取", "未标注", "未知", ""}
PARSER_VERSION = "akshare-xq-individual-v1"

# 每轮研究允许补数的基金数上限：补一只要打 2~3 次上游接口。
ENRICH_BUDGET_PER_RUN = 6


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip() in PLACEHOLDERS


def needs_enrichment(fund: Fund) -> bool:
    """关键主数据仍有占位/缺失时才值得打上游接口。"""
    return (
        _is_missing(fund.manager)
        or _is_missing(fund.company)
        or fund.scale is None
        or fund.drawdown is None
    )


def parse_scale(text: str) -> float | None:
    """「197.40亿」→ 197.4（亿元）；「5236.81万」→ 0.5237（亿元）。"""
    match = re.search(r"([\d.]+)\s*(亿|万)", str(text))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value / 10_000 if match.group(2) == "万" else value


def clean_theme(benchmark: str) -> str | None:
    """「中证白酒指数收益率×95%＋活期存款利率×5%」→「中证白酒指数」。

    业绩比较基准的指数名是最可靠的主题信号（基金名里未必带主题词）。
    """
    head = re.split(r"[×xX＊*（(]", str(benchmark))[0].strip()
    head = re.sub(r"收益率$", "", head).strip()
    if len(head) < 2 or head in PLACEHOLDERS:
        return None
    return head


def _is_na(value: Any) -> bool:
    """NaN 和 pandas NA 都算空值。

    注意 pandas NA 是三值逻辑：`value != value` 对它直接抛 TypeError，
    所以异常也算 NA。实测雪球接口的空单元格返回的是 NA 不是 NaN。
    """
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return True


def _fetch_basic(code: str) -> dict[str, str]:
    """同步阻塞，调用方放 to_thread。"""
    import akshare as ak

    frame = ak.fund_individual_basic_info_xq(symbol=code)
    items: dict[str, str] = {}
    for _, row in frame.iterrows():
        value = row["value"]
        items[str(row["item"])] = "" if _is_na(value) else str(value).strip()
    return items


def _fetch_achievement(code: str) -> list[dict[str, Any]]:
    """同步阻塞，调用方放 to_thread。"""
    import akshare as ak

    frame = ak.fund_individual_achievement_xq(symbol=code)
    return frame.to_dict("records")


def _official_drawdown(records: list[dict[str, Any]], period: str = "近1年") -> float | None:
    """雪球口径的「本产品最大回撒」是正数幅度，这里统一存成负数百分比。"""
    for record in records:
        if str(record.get("业绩类型")) == "阶段业绩" and str(record.get("周期")) == period:
            value = record.get("本产品最大回撒")
            if _is_na(value) or value is None:
                return None
            try:
                return -abs(float(value))
            except (TypeError, ValueError):
                return None
    return None


def _compute_volatility(repository: Any, code: str) -> float | None:
    """用已有净值历史算年化波动率（%）。历史拿不到就放弃，不额外打接口。"""
    records = repository.histories.get((code, "1年")) or []
    if len(records) < 30:
        return None
    navs = [r.get("nav") for r in records if isinstance(r.get("nav"), (int, float))]
    if len(navs) < 30:
        return None
    returns = [b / a - 1 for a, b in zip(navs, navs[1:], strict=False) if a]
    if len(returns) < 29:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return (variance ** 0.5) * (243 ** 0.5) * 100


async def enrich_fund(repository: Any, code: str) -> dict[str, Any] | None:
    """补一只基金：内存立即生效 + Neon 持久化。返回本次补到的字段。"""
    fund = repository.get_fund(code)
    if fund is None:
        return None

    loop = asyncio.get_running_loop()
    try:
        basic, achievement = await asyncio.gather(
            loop.run_in_executor(None, _fetch_basic, code),
            loop.run_in_executor(None, _fetch_achievement, code),
        )
    except Exception as exc:  # noqa: BLE001 - 上游接口任何失败都视为「补不到」
        logger.warning("基金 %s 补数失败：%s", code, str(exc)[:200])
        return None

    updated: dict[str, Any] = {}

    def _apply(field: str, value: Any, *, only_if_missing: bool = True) -> None:
        if value is None:
            return
        if only_if_missing and not _is_missing(getattr(fund, field)) and getattr(fund, field) is not None:
            return
        if getattr(fund, field) == value:
            return
        setattr(fund, field, value)
        updated[field] = value

    _apply("manager", basic.get("基金经理") or None)
    _apply("company", basic.get("基金公司") or None)
    _apply("theme", clean_theme(basic.get("业绩比较基准", "")))
    if fund.inception is None:
        with contextlib.suppress(KeyError, ValueError):
            _apply("inception", date.fromisoformat(basic["成立时间"]))
    if fund.scale is None:
        _apply("scale", parse_scale(basic.get("最新规模", "")))
    if fund.drawdown is None:
        _apply("drawdown", _official_drawdown(achievement))
    if fund.volatility is None:
        _apply("volatility", _compute_volatility(repository, code))

    if updated:
        # 新经理/主题要能立刻被 n-gram 检索命中
        repository._gram_index = await asyncio.to_thread(
            build_gram_index, list(repository.funds.values()),
        )
        with contextlib.suppress(Exception):  # 持久化失败只影响下次冷启动，不影响本次结果
            await _persist(fund)
    return {"code": code, "updated": updated}


async def _persist(fund: Fund) -> None:
    """把补到的字段落 Neon：profile 快照（主数据）+ metric 快照（1Y 指标）。"""
    from sqlalchemy import select

    from .db.base import FundMetricSnapshot, FundProfileSnapshot, FundRecord
    from .db.session import get_session_factory
    from .db.snapshot_repository import make_id

    now = datetime.now(UTC)
    async with get_session_factory()() as session, session.begin():
        record = await session.scalar(select(FundRecord).where(FundRecord.code == fund.code))
        if record is None:
            record = FundRecord(
                id=fund.id, code=fund.code, name=fund.name, short_name=fund.short_name,
                created_at=now, updated_at=now,
            )
            session.add(record)
            await session.flush()
        else:
            record.name = fund.name
            record.updated_at = now
        profile_id = make_id("profile")
        session.add(FundProfileSnapshot(
            id=profile_id, fund_id=record.id, provider="akshare-xq-individual",
            company=fund.company, manager=fund.manager, manager_years=fund.manager_years,
            risk_level=fund.risk, fee_rate=fund.fee, scale=fund.scale,
            subscription_status=fund.intake, theme=fund.theme,
            business_date=now.date(), fetched_at=now,
            source_type="AKSHARE_PUBLIC", trust_level="LOW",
            quality_status=fund.quality_status, parser_version=PARSER_VERSION,
        ))
        record.current_profile_snapshot_id = profile_id
        if fund.drawdown is not None or fund.volatility is not None:
            session.add(FundMetricSnapshot(
                id=make_id("metric"), fund_id=record.id, input_snapshot_id=profile_id,
                window_code="1Y", metric_version=PARSER_VERSION, calculated_at=now,
                business_date=now.date(), drawdown=fund.drawdown,
                volatility=fund.volatility, sample_count=1, status="ACTIVE",
            ))


async def load_enriched(repository: Any) -> int:
    """启动回填：把 Neon 里已有的补数快照灌回内存，避免重复打上游。

    返回回填的基金数。只补内存里仍是占位值的字段 —— 内存数据更新时不覆盖。
    """
    from sqlalchemy import text

    from .db.session import get_session_factory

    if not repository.funds:
        return 0
    async with get_session_factory()() as session:
        profile_rows = (await session.execute(text(
            "SELECT DISTINCT ON (f.code) f.code, p.company, p.manager, p.scale, p.theme,"
            " p.subscription_status"
            " FROM fund_profile_snapshots p JOIN funds f ON f.id = p.fund_id"
            " WHERE p.parser_version = :v"
            " ORDER BY f.code, p.fetched_at DESC",
        ), {"v": PARSER_VERSION})).mappings().all()
        metric_rows = (await session.execute(text(
            "SELECT DISTINCT ON (fund_id) fund_id, drawdown, volatility"
            " FROM fund_metric_snapshots WHERE window_code = '1Y'"
            " ORDER BY fund_id, calculated_at DESC",
        ))).mappings().all()
        id_rows = (await session.execute(text("SELECT id, code FROM funds"))).mappings().all()

    id_to_code = {row["id"]: row["code"] for row in id_rows}
    profiles = {row["code"]: row for row in profile_rows}
    metrics = {id_to_code.get(row["fund_id"]): row for row in metric_rows}

    changed: list[str] = []
    for code, profile in profiles.items():
        fund = repository.funds.get(code)
        if fund is None:
            continue
        touched = False
        if _is_missing(fund.manager) and profile["manager"]:
            fund.manager = profile["manager"]
            touched = True
        if _is_missing(fund.company) and profile["company"]:
            fund.company = profile["company"]
            touched = True
        if _is_missing(fund.theme) and profile["theme"]:
            fund.theme = profile["theme"]
            touched = True
        if fund.scale is None and profile["scale"] is not None:
            fund.scale = float(profile["scale"])
            touched = True
        metric = metrics.get(code)
        if metric is not None:
            if fund.drawdown is None and metric["drawdown"] is not None:
                fund.drawdown = float(metric["drawdown"])
                touched = True
            if fund.volatility is None and metric["volatility"] is not None:
                fund.volatility = float(metric["volatility"])
                touched = True
        if touched:
            changed.append(code)

    if changed:
        repository._gram_index = await asyncio.to_thread(
            build_gram_index, list(repository.funds.values()),
        )
    return len(changed)
