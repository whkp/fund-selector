from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import config_value
from .models import Fund, SourceStatus


def _positive_int(value: Any, fallback: int) -> int:
    """Coerce a config value into a positive integer, falling back on bad input."""
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


class AKShareProvider:
    """AKShare adapter. Sync AKShare calls run off the FastAPI event loop."""

    def __init__(self) -> None:
        enabled = config_value("akshare", "enabled", True, env_name="FUND_COMPASS_AKSHARE_ENABLED")
        self.enabled = str(enabled).lower() == "true"
        configured_codes = config_value(
            "akshare", "etf_codes", ["510050", "510300", "588000"], env_name="FUND_COMPASS_ETF_CODES"
        )
        self.etf_codes = (
            {str(code).strip() for code in configured_codes}
            if not isinstance(configured_codes, str)
            else {item.strip() for item in configured_codes.split(",") if item.strip()}
        )
        self.status = SourceStatus(
            source_name="AKShare public reference", source_type="AKSHARE_PUBLIC", trust_level="LOW",
            status="NOT_CONFIGURED" if not self.enabled else "PENDING", configured=self.enabled,
            license_scope="development/reference only; verify caching and redistribution rights",
            field_coverage=["fund.code", "fund.name", "fund.unitNav", "fund.navDate", "fund.metrics", "etf.quote"],
        )
        self._etf_quotes: list[dict[str, Any]] = []
        self._etf_quotes_fetched_at: datetime | None = None
        self._etf_refresh_lock = asyncio.Lock()

    async def list_funds(self) -> list[Fund]:
        if not self.enabled:
            self.status.status = "NOT_CONFIGURED"
            return []
        self.status.last_attempt_at = datetime.now(UTC)
        try:
            import akshare as ak

            directory = await asyncio.to_thread(ak.fund_name_em)
            rankings = await asyncio.to_thread(ak.fund_open_fund_rank_em, symbol="全部")
            if rankings is None or rankings.empty:
                raise RuntimeError("AKShare 基金排行返回空数据")
            # 抓取已经在 to_thread 里跑，但真正耗 CPU 的是解析：目录建索引 +
            # 500 行逐行构造 Fund。这段是纯同步计算，留在主协程会在冷启动
            # _warm_up() 和首个列表请求期间占住事件循环，把同一时刻的健康检查
            # 等请求一起拖住 —— 抓取那一步没阻塞，解析这一步却阻塞了，等于白做。
            # 整段挪进线程，只 await 一次拿结果。
            result = await asyncio.to_thread(self._parse_rankings, rankings, directory)
            if not result:
                raise RuntimeError("AKShare 基金排行未产生有效基金记录")
            self.status.status = "ACTIVE"
            self.status.last_error = ""
            self.status.last_success_at = datetime.now(UTC)
            return result
        except Exception as exc:  # noqa: BLE001 - 上游能抛什么无法穷举；错误已进 status
            self.status.status = "ERROR"
            self.status.last_error = str(exc)[:240]
            return []

    def _parse_rankings(self, rankings: Any, directory: Any) -> list[Fund]:
        """把 AKShare 的两张原始表解析成 Fund 列表。

        纯同步实现，内部没有任何 await —— **调用方负责把它放进线程**，
        见 `list_funds`。直接在主协程里调用会阻塞事件循环。
        """
        directory_by_code = {
            str(row.get("基金代码", "")).zfill(6): row
            for _, row in directory.iterrows()
        } if directory is not None else {}
        result = []
        for index, row in rankings.head(500).iterrows():
            code = str(row.get("基金代码", "")).zfill(6)
            if not code or code == "000000":
                continue
            directory_row = directory_by_code.get(code, {})
            name = str(row.get("基金简称") or directory_row.get("基金简称") or code)
            result.append(self._ranked_fund(code, name, row, directory_row, index))
        return result

    async def history(self, code: str, period: str = "1年") -> list[dict[str, Any]]:
        if not self.enabled:
            self.status.status = "NOT_CONFIGURED"
            return []
        self.status.last_attempt_at = datetime.now(UTC)
        try:
            import akshare as ak

            frame = await asyncio.to_thread(
                ak.fund_open_fund_info_em, symbol=code, indicator="单位净值走势", period=period
            )
            records = []
            for _, row in frame.iterrows():
                try:
                    nav = float(row.get("单位净值", 0))
                except (TypeError, ValueError):
                    continue
                if nav <= 0:
                    continue
                records.append({"date": str(row.get("净值日期", "")), "nav": nav,
                                "dailyChange": float(row.get("日增长率", 0) or 0)})
            records = self._limit_history_period(records, period)
            if not records:
                raise RuntimeError(f"AKShare 基金 {code} 历史净值返回空数据")
            self.status.status = "ACTIVE"
            self.status.last_error = ""
            self.status.last_success_at = datetime.now(UTC)
            return records
        except Exception as exc:  # noqa: BLE001 - 上游能抛什么无法穷举；错误已进 status
            self.status.status = "ERROR"
            self.status.last_error = str(exc)[:240]
            return []

    async def etf_quotes(self) -> list[dict[str, Any]]:
        """Fetch a small, explicitly configured set of real ETF spot quotes."""
        if not self.enabled:
            self.status.status = "NOT_CONFIGURED"
            return []
        now = datetime.now(UTC)
        if self._etf_quotes_fetched_at and (now - self._etf_quotes_fetched_at).total_seconds() < 60:
            return self._etf_quotes
        async with self._etf_refresh_lock:
            now = datetime.now(UTC)
            if self._etf_quotes_fetched_at and (now - self._etf_quotes_fetched_at).total_seconds() < 60:
                return self._etf_quotes
            self.status.last_attempt_at = now
            try:
                import akshare as ak

                frame = await asyncio.to_thread(ak.fund_etf_spot_em)
                codes = self.etf_codes

                def number(row: Any, column: str) -> float:
                    try:
                        value = row.get(column, 0)
                        return 0.0 if value != value else float(value)
                    except (TypeError, ValueError):
                        return 0.0

                items = []
                for _, row in frame.iterrows():
                    code = str(row.get("代码", "")).zfill(6)
                    if code not in codes:
                        continue
                    items.append({
                        "code": code, "name": str(row.get("名称", code)), "venue": "ETF",
                        "marketPrice": number(row, "最新价"), "previousClose": number(row, "昨收"),
                        "changePercent": number(row, "涨跌幅"), "amountWan": number(row, "成交额") / 10_000,
                        "turnoverPercent": number(row, "换手率"), "asOf": now.isoformat(),
                        "sourceName": "AKShare ETF public reference", "sourceType": "EXCHANGE_QUOTE",
                        "trustLevel": "LOW", "freshnessStatus": "ACTIVE",
                        "snapshotId": f"akshare-etf-{now.strftime('%Y%m%d%H%M')}",
                    })
                self._etf_quotes = items
                self._etf_quotes_fetched_at = now
                self.status.status = "ACTIVE"
                self.status.last_error = ""
                self.status.last_success_at = now
                return items
            except Exception as exc:  # noqa: BLE001 - 上游能抛什么无法穷举；错误已进 status
                self.status.status = "ERROR"
                self.status.last_error = str(exc)[:240]
                return self._etf_quotes

    @staticmethod
    def _limit_history_period(records: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
        """Enforce a requested history window when upstream returns full history."""
        days_by_period = {"1月": 31, "3月": 92, "6月": 183, "1年": 365, "3年": 365 * 3}
        days = days_by_period.get(period)
        if not days or not records:
            return records
        parsed = []
        for record in records:
            try:
                parsed.append((datetime.fromisoformat(record["date"]).date(), record))
            except (KeyError, TypeError, ValueError):
                continue
        if not parsed:
            return []
        latest_date = max(item[0] for item in parsed)
        cutoff = latest_date - timedelta(days=days)
        return [record for record_date, record in parsed if record_date >= cutoff]

    def _ranked_fund(self, code: str, name: str, row: Any, directory_row: Any, index: Any) -> Fund:
        def number(key: str) -> float | None:
            value = row.get(key, None)
            try:
                if value != value:  # pandas NaN
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        fee_text = str(row.get("手续费", "")).replace("%", "")
        try:
            fee = float(fee_text) if fee_text else None
        except ValueError:
            fee = None
        nav = number("单位净值")
        nav_date = str(row.get("日期", ""))
        type_name = str(directory_row.get("基金类型", "未知"))
        one_year = number("近1年")
        three_month = number("近3月")
        # This is an explicit ordering heuristic based only on returned AKShare fields,
        # not a recommendation score or a substituted third-party value.
        score = max(0, min(100, round(
            50 + (one_year or 0) * 0.18 - max((one_year or 0) - (three_month or 0), 0) * 0.04
            - max((fee or 0) - 0.5, 0) * 5
        )))
        return Fund(
            id=f"ak-{code}", code=code, name=name, short_name=name[:16], type=type_name,
            risk="未获取", manager="未获取", manager_years=None, company="未获取", theme="未标注",
            nav=nav, accumulated_nav=number("累计净值"), nav_date=nav_date,
            ytd=number("今年来"), one_year=one_year, volatility=None, drawdown=None,
            fee=fee, scale=None, inception=None, score=score, score_parts=[],
            reason="按 AKShare 当前公开排行字段排序；未获得的主数据不参与计算。",
            caveat="风险等级、基金经理、规模和申购状态尚未由当前数据入口提供，需进一步核验。",
            highlights=["单位净值已获取", "区间收益已获取"], status="待核",
            source="AKShare public reference", snapshot=f"akshare-rank-{nav_date}", chart=[], tags=[],
            intake="未获取", quality_status="REFERENCE", nav_source_type="AKSHARE_PUBLIC",
            nav_trust_level="LOW", nav_freshness="REFERENCE",
        )


class DataRepository:
    """In-memory repository with TTL-bounded caches.

    The public reference dataset is daily-frequency upstream data, so entries are
    kept for a configurable window and refetched once stale. Without a TTL the
    process would serve the first snapshot for its entire lifetime.
    """

    def __init__(self, provider: AKShareProvider) -> None:
        self.provider = provider
        self.funds: dict[str, Fund] = {}
        self.histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.watchlist: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.funds_ttl_seconds = _positive_int(
            config_value("repository", "funds_ttl_seconds", 21600, env_name="FUND_COMPASS_FUNDS_TTL_SECONDS"),
            21600,
        )
        self.history_ttl_seconds = _positive_int(
            config_value("repository", "history_ttl_seconds", 21600, env_name="FUND_COMPASS_HISTORY_TTL_SECONDS"),
            21600,
        )
        self._funds_fetched_at: datetime | None = None
        self._history_fetched_at: dict[tuple[str, str], datetime] = {}
        self._funds_lock = asyncio.Lock()

    async def refresh_funds(self) -> bool:
        items = await self.provider.list_funds()
        if not items:
            return False
        # Replace the universe atomically so a failed or partial refresh cannot
        # mix stale records into the current public reference dataset.
        self.funds = {item.code: item for item in items}
        self.histories.clear()
        self._history_fetched_at.clear()
        self._funds_fetched_at = datetime.now(UTC)
        return True

    def _funds_are_fresh(self) -> bool:
        if not self.funds or self._funds_fetched_at is None:
            return False
        age = (datetime.now(UTC) - self._funds_fetched_at).total_seconds()
        return age < self.funds_ttl_seconds

    async def ensure_funds(self) -> bool:
        """Serve the cached universe while fresh, refetch once it expires.

        Concurrent callers share a single upstream refresh. When the upstream
        fails but a previous snapshot exists, the stale snapshot is served and the
        retry is deferred to the next TTL window instead of hammering the source.
        """
        if self._funds_are_fresh():
            return True
        async with self._funds_lock:
            if self._funds_are_fresh():
                return True
            if await self.refresh_funds():
                return True
            if self.funds:
                self._funds_fetched_at = datetime.now(UTC)
                return True
            return False

    async def history(self, code: str, period: str) -> list[dict[str, Any]]:
        cache_key = (code, period)
        fetched_at = self._history_fetched_at.get(cache_key)
        if (
            cache_key in self.histories
            and fetched_at is not None
            and (datetime.now(UTC) - fetched_at).total_seconds() < self.history_ttl_seconds
        ):
            return self.histories[cache_key]
        records = await self.provider.history(code, period)
        if not records and cache_key in self.histories:
            # Keep the last good window and defer the retry instead of caching a failure.
            self._history_fetched_at[cache_key] = datetime.now(UTC)
            return self.histories[cache_key]
        self.histories[cache_key] = records
        self._history_fetched_at[cache_key] = datetime.now(UTC)
        return records

    def list_funds(self) -> list[Fund]:
        return sorted(self.funds.values(), key=lambda item: (-item.score, item.code))

    def get_fund(self, code: str) -> Fund | None:
        return self.funds.get(code)

    def add_watch(self, fund: Fund, note: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        item = {"fund": fund, "note": note, "reasonTags": tags or [], "addedAt": datetime.now(UTC).isoformat()}
        self.watchlist[fund.code] = item
        return item
