from __future__ import annotations

from math import sqrt
from typing import Iterable


def returns(values: Iterable[float]) -> list[float]:
    values = list(values)
    return [(values[i] / values[i - 1]) - 1 for i in range(1, len(values)) if values[i - 1] > 0]


def calculate_metrics(values: Iterable[float], annual_days: int = 252) -> dict[str, float | str | int]:
    prices = [float(value) for value in values if float(value) > 0]
    daily = returns(prices)
    if len(prices) < 2:
        return {"status": "INSUFFICIENT_HISTORY", "sampleCount": len(prices)}
    total_return = (prices[-1] / prices[0] - 1) * 100
    peak = prices[0]
    max_drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        max_drawdown = min(max_drawdown, (price / peak - 1) * 100)
    volatility = (sum((item - sum(daily) / len(daily)) ** 2 for item in daily) / (len(daily) - 1)) ** 0.5 * sqrt(annual_days) * 100 if len(daily) > 1 else 0.0
    annual_return = ((prices[-1] / prices[0]) ** (annual_days / max(len(prices) - 1, 1)) - 1) * 100
    sharpe = ((annual_return / 100) - 0.015) / (volatility / 100) if volatility > 0 else 0.0
    return {
        "status": "PASS", "sampleCount": len(prices), "totalReturn": round(total_return, 4),
        "annualReturn": round(annual_return, 4), "volatility": round(volatility, 4),
        "drawdown": round(max_drawdown, 4), "sharpe": round(sharpe, 4), "metricVersion": "metric-v1",
    }
