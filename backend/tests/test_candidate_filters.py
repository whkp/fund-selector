"""候选过滤语义的回归测试。

背景：AKShare 的公开排行接口（`fund_open_fund_rank_em`）只返回收益前 500 名，
而且不提供风险等级、申购状态与成立年限。修复前有两处会静默清空候选集：

1. 类型用精确相等比较 —— 数据侧返回的是 "混合型-偏股"，界面给的是 "混合型"，
   于是每一次类型筛选都返回空集；
2. 缺失字段被当成不合格 —— 风险等级/申购状态/成立年限全是"未获取"，
   于是「最高风险等级」「仅看开放申购」「期限 3 年」三个默认开启的条件
   把 500 只基金全部滤掉。

界面侧的表现就是「暂未得到研究候选」，且看不出是哪一条约束造成的。
这里把规则固定下来：**缺失不排除（只标注未核验），类型按根段匹配。**
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ["FUND_COMPASS_AKSHARE_ENABLED"] = "false"
os.environ["FUND_COMPASS_MODE"] = "REFERENCE"

from app.llm import ModelAssessment, ModelResearchOutput
from app.main import Filters, app, eligibility, llm_service, repository, type_matches
from app.models import Fund

client = TestClient(app)
INVITE_CODE = os.environ["FUND_COMPASS_INVITE_CODE"]


def _filters(**kwargs) -> Filters:
    return Filters(**kwargs)


def make_fund(code: str, fund_type: str, *, risk: str = "未获取", fee: float | None = 0.15,
              intake: str = "未获取", inception: float | None = None, score: int = 70) -> Fund:
    """默认值刻意对齐 AKShare 公开排行的真实返回：三个主数据字段都是"未获取"。"""
    return Fund(
        id=f"ak-{code}", code=code, name=f"测试基金 {code}", short_name=f"基金 {code}",
        type=fund_type, risk=risk, manager="未获取", manager_years=None, company="未获取",
        theme="未标注", nav=1.0, nav_date="2026-09-11", ytd=10.0, one_year=12.0,
        volatility=None, drawdown=None, fee=fee, scale=None, inception=inception, score=score,
        score_parts=[], reason="按 AKShare 当前公开排行字段排序。", caveat="主数据待核验。",
        highlights=["单位净值已获取"], status="待核", source="AKShare public reference",
        snapshot="akshare-test-snapshot", chart=[], tags=[], intake=intake,
        quality_status="REFERENCE", nav_source_type="AKSHARE_PUBLIC",
        nav_trust_level="LOW", nav_freshness="REFERENCE",
    )


@pytest.fixture(autouse=True)
def akshare_like_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    """复刻真实数据源的类型分布：500 只里没有一只叫"混合型"。"""
    repository.funds = {
        fund.code: fund for fund in [
            make_fund("008286", "混合型-偏股", score=82),
            make_fund("012349", "混合型-灵活", score=78),
            make_fund("012861", "指数型-股票", score=69),
            make_fund("004640", "股票型", risk="高风险", fee=1.5, intake="限额申购", score=57),
            make_fund("017327", "QDII-混合偏股", risk="中风险", intake="暂停申购", score=75),
        ]
    }
    repository.histories = {}
    repository.watchlist = {}
    repository.runs = {}

    async def fake_research(query: str, funds: list[Fund], knowledge: list[object], limit: int) -> ModelResearchOutput:
        return ModelResearchOutput(
            intent=query, themes=[], ambiguities=[], summary="测试结论。",
            ranking=[ModelAssessment(fundCode=fund.code, score=88 - index, fit="匹配", reason="测试", riskFlags=[])
                     for index, fund in enumerate(funds[:limit])],
            followUpQuestions=[],
        )

    monkeypatch.setattr(llm_service, "research", fake_research)


def register_user(prefix: str = "filter") -> dict[str, str]:
    response = client.post("/api/auth/register", json={
        "email": f"{prefix}_{uuid.uuid4().hex[:10]}@example.com",
        "password": "test-password-123",
        "inviteCode": INVITE_CODE,
    })
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def screen(codes_filter: dict, headers: dict[str, str]) -> list[str]:
    response = client.post("/api/funds/screen", json={
        "query": "新能源", "limit": 30, "filters": codes_filter,
    }, headers=headers)
    assert response.status_code == 200, response.text
    return [item["code"] for item in response.json()["items"]]


# --- 类型匹配 ---------------------------------------------------------------

def test_composite_type_names_match_their_root_category():
    """"混合型" 必须匹配 "混合型-偏股" / "混合型-灵活"，但不能吞掉 QDII。"""
    assert type_matches("混合型-偏股", ["混合型"]) is True
    assert type_matches("混合型-灵活", ["混合型"]) is True
    assert type_matches("混合型", ["混合型"]) is True
    assert type_matches("指数型-海外股票", ["指数型"]) is True
    # QDII-混合偏股 的根是 QDII，不该被"混合型"收进来。
    assert type_matches("QDII-混合偏股", ["混合型"]) is False
    # 没有筛选条件时全部放行。
    assert type_matches("股票型", []) is True
    assert type_matches("股票型", [""]) is True


def test_screen_by_mixed_type_returns_candidates_instead_of_empty_set():
    """回归：修复前这一步必然返回空数组，界面显示「暂未得到研究候选」。"""
    codes = screen({"fundTypes": ["混合型"]}, register_user())
    assert codes == ["008286", "012349"]


def test_screen_by_index_type_excludes_mixed_and_qdii():
    assert screen({"fundTypes": ["指数型"]}, register_user()) == ["012861"]


def test_debt_type_truthfully_reports_no_match_with_actionable_error():
    """数据源里没有债券型时要明说，而不是丢一个空的候选列表。"""
    response = client.post("/api/recommendations/runs", json={
        "query": "纯债", "limit": 5, "filters": {"fundTypes": ["债券型"]},
    }, headers=register_user())
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "NO_ELIGIBLE_CANDIDATES"
    assert "债券型" in detail["message"]
    assert "混合型" in detail["message"]  # 顺带告知现有类型，用户知道能选什么


# --- 缺失字段不排除 ---------------------------------------------------------

def test_unknown_fields_do_not_empty_the_pool():
    """回归：三个默认开启的条件在真实数据上会把候选全部滤掉。"""
    codes = screen({
        "riskLevelMax": "中风险", "maxFee": 1.2, "requireOpen": True, "minimumInceptionYears": 3,
    }, register_user())
    # 004640 风险已知且超上限 -> 排除；017327 申购状态已知且非开放 -> 排除。
    assert codes == ["008286", "012349", "012861"]


def test_known_values_are_still_enforced():
    """宽松只针对"未获取"；字段一旦有值，约束照旧生效。"""
    fund = make_fund("000001", "股票型", risk="高风险", intake="暂停申购", inception=1.5)
    passed, unverified = eligibility(fund, _filters(riskLevelMax="中风险", requireOpen=True, minimumInceptionYears=3))
    assert passed is False
    assert unverified == []  # 有真实值参与判定，不算未核验

    sole_inception = make_fund("000002", "股票型", inception=1.5)
    passed, unverified = eligibility(sole_inception, _filters(minimumInceptionYears=3))
    assert passed is False
    assert unverified == []


def test_unverified_fields_are_reported_not_silently_ignored():
    fund = make_fund("000003", "混合型-偏股")
    passed, unverified = eligibility(fund, _filters(riskLevelMax="中风险", requireOpen=True, minimumInceptionYears=3))
    assert passed is True
    assert set(unverified) == {"风险等级", "申购状态", "成立年限"}


def test_research_trace_discloses_which_constraints_could_not_apply():
    """用户必须能从研究记录里看出"这几个条件其实没生效"，否则会误判筛选结果。"""
    response = client.post("/api/recommendations/runs", json={
        "query": "长期定投", "limit": 3,
        "filters": {"riskLevelMax": "中高风险", "maxFee": 1.2, "requireOpen": True},
    }, headers=register_user())
    assert response.status_code == 200
    trace = response.json()["trace"]
    candidate_event = next(event for event in trace if event["event"] == "tool_completed")
    assert "未参与筛除" in candidate_event["detail"]
    assert "风险等级" in candidate_event["detail"]
    assert "申购状态" in candidate_event["detail"]


def test_failed_research_leaves_no_phantom_conversation(monkeypatch: pytest.MonkeyPatch):
    """研究失败时不能在「复盘日志」里留下一问无答的空对话。"""
    from app.llm import LLMNotConfigured

    class UnconfiguredService:
        provider = "openai-compatible"
        model = "gpt-4o-mini"

        async def research(self, *args: object, **kwargs: object) -> ModelResearchOutput:
            raise LLMNotConfigured("本次会话模型未配置")

    monkeypatch.setattr(llm_service, "for_session_request", lambda overrides: UnconfiguredService())
    headers = register_user("phantom")
    before = client.get("/api/conversations", headers=headers).json()["items"]

    response = client.post("/api/recommendations/runs", json={
        "query": "这一步会失败", "limit": 3, "filters": {},
        "llm": {"provider": "openai-compatible", "baseUrl": "https://api.deepseek.com/v1",
                "apiKey": "placeholder", "model": "deepseek-chat"},
    }, headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "LLM_NOT_CONFIGURED"
    assert client.get("/api/conversations", headers=headers).json()["items"] == before


def test_rejected_candidate_pool_leaves_no_phantom_conversation():
    headers = register_user("phantompool")
    response = client.post("/api/recommendations/runs", json={
        "query": "纯债", "limit": 3, "filters": {"fundTypes": ["债券型"]},
    }, headers=headers)
    assert response.status_code == 400
    assert client.get("/api/conversations", headers=headers).json()["items"] == []


def test_trace_is_clean_when_every_field_is_known():
    """字段齐全时不应该出现多余告警。"""
    repository.funds = {fund.code: fund for fund in [
        make_fund("100001", "混合型-偏股", risk="中风险", intake="开放申购", inception=5.0),
    ]}
    response = client.post("/api/recommendations/runs", json={
        "query": "长期定投", "limit": 3,
        "filters": {"riskLevelMax": "中高风险", "requireOpen": True, "minimumInceptionYears": 3},
    }, headers=register_user())
    assert response.status_code == 200
    detail = next(event for event in response.json()["trace"] if event["event"] == "tool_completed")["detail"]
    assert "未参与筛除" not in detail
