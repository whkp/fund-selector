"""基金多维评分引擎（score-v1）。

设计原则：
- 纯函数，无 IO，不依赖 asyncio，可安全地在线程池里调用。
- 每个维度独立归一化到 0~100，缺字段时透明降级（不计入加权分母）。
- 总分 = sum(value × weight) / sum(有效 weight)，再取整到 0~100。
- 权重在 config/fund-compass.json 的 scoring.weights 里可调；缺失时用下方默认值。
- 输出 score_parts 供前端逐维度展示，字段 raw 给原始数值文字，basis 说明数据来源。

版本：score-v1
"""
from __future__ import annotations

import contextlib
from typing import Any

from .config import config_value
from .models import Fund

SCORE_VERSION = "score-v1"

# 默认维度权重（可通过 config scoring.weights 覆盖）
DEFAULT_WEIGHTS: dict[str, float] = {
    "one_year":      0.30,
    "drawdown":      0.25,
    "fee":           0.20,
    "scale":         0.15,
    "manager_years": 0.10,
}

# 维度显示名与颜色（与前端 CSS 变量对应）
_DIM_META: dict[str, tuple[str, str]] = {
    "one_year":      ("近1年收益", "#1a3358"),
    "drawdown":      ("风险控制",  "#14704f"),
    "fee":           ("成本效率",  "#a9822f"),
    "scale":         ("规模健康",  "#2a4d80"),
    "manager_years": ("经理经验",  "#3d6b5e"),
}

_UNKNOWN = {"", "未获取", "未知", "未标注", "待核", "none", "null", "n/a", "-"}


def _is_known(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in _UNKNOWN


def _scoring_weights() -> dict[str, float]:
    """从配置读取权重，缺失维度补默认值。"""
    cfg = config_value("scoring", "weights", {})
    if not isinstance(cfg, dict):
        return DEFAULT_WEIGHTS.copy()
    merged = DEFAULT_WEIGHTS.copy()
    for k, v in cfg.items():
        if k in merged:
            with contextlib.suppress(TypeError, ValueError):
                merged[k] = float(v)
    return merged


# ---------------------------------------------------------------------------
# 各维度归一化函数：输入原始值，输出 0~100（float），同时返回 raw 文字
# ---------------------------------------------------------------------------

def _norm_one_year(value: float, universe_values: list[float] | None) -> tuple[float, str]:
    """近1年收益：全集百分位；无全集时按 [-30%, +50%] 绝对值插值。"""
    raw = f"{value:+.2f}%"
    if universe_values and len(universe_values) >= 5:
        rank = sum(1 for v in universe_values if v <= value)
        score = rank / len(universe_values) * 100.0
    else:
        # 兜底：-30% → 0，+50% → 100，线性插值
        score = max(0.0, min(100.0, (value + 30.0) / 80.0 * 100.0))
    return score, raw


def _norm_drawdown(value: float, universe_values: list[float] | None) -> tuple[float, str]:
    """最大回撤：回撤越小越高。-5% → 100，-50% → 0（值为负数）。"""
    raw = f"{value:+.2f}%"
    if universe_values and len(universe_values) >= 5:
        # 回撤越大（更负）排名越靠后，用反序百分位
        rank = sum(1 for v in universe_values if v >= value)
        score = rank / len(universe_values) * 100.0
    else:
        # 兜底：-5% → 100，-50% → 0
        score = max(0.0, min(100.0, (value + 50.0) / 45.0 * 100.0))
    return score, raw


def _norm_fee(value: float) -> tuple[float, str]:
    """管理费：越低越高。0.10% → 100，1.50% → 0。"""
    raw = f"{value:.2f}%"
    score = max(0.0, min(100.0, (1.50 - value) / 1.40 * 100.0))
    return score, raw


def _norm_scale(value: float) -> tuple[float, str]:
    """规模：2~50 亿区间满分，过小/过大折减。单位：亿。"""
    if value < 0.01:
        raw = f"{value * 100:.0f} 百万"
    elif value >= 10000:
        raw = f"{value / 10000:.1f} 万亿"
    else:
        raw = f"{value:.1f} 亿"
    if value < 0.5:
        score = value / 0.5 * 40.0          # 极小规模：清盘风险，最高 40 分
    elif value < 2.0:
        score = 40.0 + (value - 0.5) / 1.5 * 40.0   # 2 亿以下：流动性不足
    elif value <= 50.0:
        score = 80.0 + (value - 2.0) / 48.0 * 20.0  # 2~50 亿：合理区间
    elif value <= 200.0:
        score = 100.0 - (value - 50.0) / 150.0 * 20.0  # 偏大：规模效应开始减弱
    else:
        score = max(60.0, 80.0 - (value - 200.0) / 800.0 * 20.0)  # 超大：仓位灵活度下降
    return score, raw


def _norm_manager_years(value: float) -> tuple[float, str]:
    """基金经理任职年限：≥5 年满分，线性折减到 0。"""
    raw = f"{value:.1f} 年"
    score = max(0.0, min(100.0, value / 5.0 * 100.0))
    return score, raw


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_score(
    fund: Fund,
    universe: list[Fund] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """计算基金综合排序分。

    Args:
        fund: 被评分的基金。
        universe: 可选全集，用于百分位归一化；不传时用绝对值兜底。

    Returns:
        (总分 0-100, score_parts 列表)，总分已 round 取整。
    """
    weights = _scoring_weights()

    # 提前算好全集分布，避免内层重复过滤
    universe_one_year: list[float] = []
    universe_drawdown: list[float] = []
    if universe:
        universe_one_year = [f.one_year for f in universe if isinstance(f.one_year, (int, float))]
        universe_drawdown = [f.drawdown for f in universe if isinstance(f.drawdown, (int, float))]

    parts: list[dict[str, Any]] = []

    # ---- 近1年收益 ----
    label, color = _DIM_META["one_year"]
    w = weights["one_year"]
    if isinstance(fund.one_year, (int, float)):
        v, raw = _norm_one_year(fund.one_year, universe_one_year or None)
        parts.append({"label": label, "value": round(v), "weight": w, "color": color,
                      "raw": raw, "basis": "AKShare 排行字段"})
    else:
        parts.append({"label": label, "value": None, "weight": w, "color": color,
                      "raw": "--", "basis": "未获取"})

    # ---- 风险控制（最大回撤） ----
    label, color = _DIM_META["drawdown"]
    w = weights["drawdown"]
    drawdown_val = fund.drawdown
    if not isinstance(drawdown_val, (int, float)) and isinstance(fund.volatility, (int, float)):
        # 无回撤时用波动率代替（反向，波动率越低越好）
        drawdown_val = -fund.volatility  # 折算成负数兜底
    if isinstance(drawdown_val, (int, float)):
        v, raw = _norm_drawdown(drawdown_val, universe_drawdown or None)
        basis = "AKShare 排行字段" if isinstance(fund.drawdown, (int, float)) else "以波动率代替"
        parts.append({"label": label, "value": round(v), "weight": w, "color": color,
                      "raw": raw, "basis": basis})
    else:
        parts.append({"label": label, "value": None, "weight": w, "color": color,
                      "raw": "--", "basis": "未获取"})

    # ---- 成本效率（管理费） ----
    label, color = _DIM_META["fee"]
    w = weights["fee"]
    if isinstance(fund.fee, (int, float)):
        v, raw = _norm_fee(fund.fee)
        parts.append({"label": label, "value": round(v), "weight": w, "color": color,
                      "raw": raw, "basis": "AKShare 排行字段"})
    else:
        parts.append({"label": label, "value": None, "weight": w, "color": color,
                      "raw": "--", "basis": "未获取"})

    # ---- 规模健康 ----
    label, color = _DIM_META["scale"]
    w = weights["scale"]
    if isinstance(fund.scale, (int, float)) and fund.scale > 0:
        v, raw = _norm_scale(fund.scale)
        parts.append({"label": label, "value": round(v), "weight": w, "color": color,
                      "raw": raw, "basis": "雪球补数字段"})
    else:
        parts.append({"label": label, "value": None, "weight": w, "color": color,
                      "raw": "--", "basis": "未获取"})

    # ---- 经理经验 ----
    label, color = _DIM_META["manager_years"]
    w = weights["manager_years"]
    if isinstance(fund.manager_years, (int, float)) and fund.manager_years >= 0:
        v, raw = _norm_manager_years(fund.manager_years)
        parts.append({"label": label, "value": round(v), "weight": w, "color": color,
                      "raw": raw, "basis": "雪球补数字段"})
    else:
        parts.append({"label": label, "value": None, "weight": w, "color": color,
                      "raw": "--", "basis": "未获取"})

    # ---- 加权合计（跳过 null 维度） ----
    valid_parts = [p for p in parts if p["value"] is not None]
    if not valid_parts:
        # 全部字段缺失：兜底用 50 分
        return 50, parts

    total_weight = sum(p["weight"] for p in valid_parts)
    if total_weight <= 0:
        return 50, parts

    weighted_sum = sum(p["value"] * p["weight"] for p in valid_parts)
    final_score = max(0, min(100, round(weighted_sum / total_weight)))
    return final_score, parts


def build_reason(fund: Fund, score_parts: list[dict[str, Any]]) -> str:
    """用评分维度拼装新手可读的一句话结论。

    取前两条有效维度的原始数据，拼成自然语言，例如：
    '近1年收益 +18.4%，回撤控制良好（-12.3%），费率 0.6% 低于同类均值。'
    """
    valid = [p for p in score_parts if p["value"] is not None and p["raw"] != "--"]
    # 按 value 降序取最突出的维度
    valid.sort(key=lambda p: p["value"], reverse=True)  # type: ignore[arg-type]

    if not valid:
        return "数据待补充，AI 研究时会自动获取关键字段。"

    pieces: list[str] = []
    for part in valid[:3]:
        label, raw, val = part["label"], part["raw"], part["value"]
        if label == "近1年收益":
            pieces.append(f"近1年收益 {raw}")
        elif label == "风险控制":
            level = "优秀" if val >= 75 else ("良好" if val >= 50 else "偏高")
            pieces.append(f"回撤控制{level}（{raw}）")
        elif label == "成本效率":
            level = "较低" if val >= 70 else ("适中" if val >= 40 else "偏高")
            pieces.append(f"费率 {raw} {level}")
        elif label == "规模健康":
            pieces.append(f"规模 {raw}")
        elif label == "经理经验":
            pieces.append(f"经理任职 {raw}")

    if not pieces:
        return "按 AKShare 公开排行字段综合排序。"

    return "，".join(pieces) + "。"
