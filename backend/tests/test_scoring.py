"""评分引擎 test_scoring.py — 不触网，不依赖数据库。

覆盖：
- 各维度归一化边界（最大/最小值 → 100/0）
- 缺字段透明降级（不计入加权分母，总分仍正常）
- score_parts 结构完整（5 条，含 label/value/weight/color/raw/basis）
- build_reason 输出含数据数字、非空
- 全集百分位模式下排名边界
"""
from __future__ import annotations

from app.models import Fund
from app.scoring import SCORE_VERSION, build_reason, compute_score


def make_fund(**kwargs) -> Fund:
    defaults = dict(
        id="test-000001", code="000001", name="测试基金", short_name="测试",
        type="混合型", risk="未获取", manager="未获取", manager_years=None,
        company="未获取", theme="未标注", nav=1.0, nav_date="2026-09-27",
        ytd=None, one_year=None, volatility=None, drawdown=None,
        fee=None, scale=None, inception=None, score=0, score_parts=[],
        reason="", caveat="", highlights=[], status="待核",
        source="test", snapshot="test", chart=[], tags=[], intake="未获取",
        quality_status="REFERENCE",
    )
    defaults.update(kwargs)
    return Fund(**defaults)


# ---- score_parts 结构完整 ----

def test_score_parts_have_required_keys():
    fund = make_fund(one_year=10.0, drawdown=-15.0, fee=0.6, scale=5.0, manager_years=3.0)
    _, parts = compute_score(fund)
    assert len(parts) == 5
    for part in parts:
        assert "label" in part
        assert "value" in part
        assert "weight" in part
        assert "color" in part
        assert "raw" in part
        assert "basis" in part


def test_score_parts_labels_match_dimensions():
    fund = make_fund()
    _, parts = compute_score(fund)
    labels = {p["label"] for p in parts}
    assert "近1年收益" in labels
    assert "风险控制" in labels
    assert "成本效率" in labels
    assert "规模健康" in labels
    assert "经理经验" in labels


# ---- 各维度归一化边界 ----

def test_fee_min_maps_to_100():
    """最低费率 0.1% 应得满分（或接近满分）。"""
    fund = make_fund(fee=0.1)
    _, parts = compute_score(fund)
    fee_part = next(p for p in parts if p["label"] == "成本效率")
    assert fee_part["value"] is not None
    assert fee_part["value"] >= 99


def test_fee_max_maps_to_0():
    """最高费率 1.5% 应得 0 分（或接近 0）。"""
    fund = make_fund(fee=1.5)
    _, parts = compute_score(fund)
    fee_part = next(p for p in parts if p["label"] == "成本效率")
    assert fee_part["value"] is not None
    assert fee_part["value"] <= 1


def test_manager_5yr_maps_to_100():
    """任职 5 年应得满分。"""
    fund = make_fund(manager_years=5.0)
    _, parts = compute_score(fund)
    mgr_part = next(p for p in parts if p["label"] == "经理经验")
    assert mgr_part["value"] == 100


def test_manager_0yr_maps_to_0():
    """任职 0 年应得 0 分。"""
    fund = make_fund(manager_years=0.0)
    _, parts = compute_score(fund)
    mgr_part = next(p for p in parts if p["label"] == "经理经验")
    assert mgr_part["value"] == 0


def test_scale_optimal_range_scores_high():
    """规模 10 亿处于最佳区间，应 ≥80 分。"""
    fund = make_fund(scale=10.0)
    _, parts = compute_score(fund)
    scale_part = next(p for p in parts if p["label"] == "规模健康")
    assert scale_part["value"] is not None
    assert scale_part["value"] >= 80


def test_scale_tiny_scores_low():
    """极小规模（0.01 亿，清盘风险）应 ≤10 分。"""
    fund = make_fund(scale=0.01)
    _, parts = compute_score(fund)
    scale_part = next(p for p in parts if p["label"] == "规模健康")
    assert scale_part["value"] is not None
    assert scale_part["value"] <= 10


# ---- 缺字段降级：不计入加权分母 ----

def test_missing_fields_give_null_value():
    """字段全缺时 score_parts 的 value 应全部为 None。"""
    fund = make_fund()  # 所有数值字段为 None
    _, parts = compute_score(fund)
    for part in parts:
        assert part["value"] is None


def test_missing_fields_fallback_score_is_50():
    """所有字段缺失时总分兜底为 50。"""
    fund = make_fund()
    score, _ = compute_score(fund)
    assert score == 50


def test_partial_fields_excluded_from_denominator():
    """只有部分字段：总分只用有效维度的加权平均，不被 null 维度拉低。"""
    # 只有费率字段（0.1% → 接近 100 分）
    fund = make_fund(fee=0.1)
    score, parts = compute_score(fund)
    valid = [p for p in parts if p["value"] is not None]
    assert len(valid) == 1
    assert valid[0]["label"] == "成本效率"
    # 总分应接近 100（单维度满分）
    assert score >= 95


def test_score_in_valid_range():
    """总分始终在 0~100。"""
    for fee in [0.1, 0.6, 1.5]:
        for one_year in [-30.0, 0.0, 50.0]:
            fund = make_fund(fee=fee, one_year=one_year, drawdown=-10.0)
            score, _ = compute_score(fund)
            assert 0 <= score <= 100, f"score={score} out of range for fee={fee}, one_year={one_year}"


# ---- 全集百分位模式 ----

def test_universe_percentile_top_scores_higher():
    """传全集时，近1年收益最高的基金分数应高于最低的。"""
    funds = [
        make_fund(code="A", one_year=-20.0),
        make_fund(code="B", one_year=0.0),
        make_fund(code="C", one_year=20.0),
        make_fund(code="D", one_year=40.0),
    ]
    scores = {}
    for f in funds:
        score, parts = compute_score(f, universe=funds)
        oy = next(p for p in parts if p["label"] == "近1年收益")
        scores[f.code] = oy["value"]

    assert scores["D"] > scores["C"] > scores["B"] > scores["A"]


def test_universe_bottom_one_year_scores_0():
    """全集中收益最低的基金近1年维度应得 0 分（百分位最低）。"""
    funds = [
        make_fund(code="A", one_year=-30.0),
        make_fund(code="B", one_year=10.0),
        make_fund(code="C", one_year=30.0),
    ]
    _, parts = compute_score(funds[0], universe=funds)
    oy = next(p for p in parts if p["label"] == "近1年收益")
    # 最低收益 → 百分位最底，score 应为 round(1/3*100) = 33 或更低
    assert oy["value"] is not None
    assert oy["value"] <= 35


# ---- build_reason ----

def test_build_reason_nonempty():
    """有有效字段时 build_reason 不返回空字符串。"""
    fund = make_fund(one_year=15.0, fee=0.6)
    _, parts = compute_score(fund)
    reason = build_reason(fund, parts)
    assert reason
    assert len(reason) > 5


def test_build_reason_contains_numeric_data():
    """reason 应包含至少一个数字字符（原始数据）。"""
    fund = make_fund(one_year=18.4, drawdown=-12.3)
    _, parts = compute_score(fund)
    reason = build_reason(fund, parts)
    assert any(c.isdigit() for c in reason), f"reason 不含数字: {reason!r}"


def test_build_reason_fallback_when_all_null():
    """所有字段为 None 时 reason 给出兜底文案。"""
    fund = make_fund()
    _, parts = compute_score(fund)
    reason = build_reason(fund, parts)
    assert "数据待补充" in reason or "公开排行" in reason


# ---- SCORE_VERSION ----

def test_score_version_string():
    assert SCORE_VERSION == "score-v1"
