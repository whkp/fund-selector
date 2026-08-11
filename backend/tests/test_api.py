import os

import pytest
from fastapi.testclient import TestClient

# Tests must be deterministic and must never call the public provider.
os.environ["FUND_COMPASS_AKSHARE_ENABLED"] = "false"
os.environ["FUND_COMPASS_MODE"] = "REFERENCE"

from app.llm import ModelAssessment, ModelResearchOutput
from app.main import app, llm_service, repository
from app.models import Fund


def reference_fund(code: str, score: int, *, risk: str = "中高风险", fee: float | None = 1.2,
                   intake: str = "开放申购") -> Fund:
    return Fund(
        id=f"ak-{code}", code=code, name=f"AKShare 基金 {code}", short_name=f"基金 {code}",
        type="混合型", risk=risk, manager="未获取", manager_years=None, company="未获取",
        theme="未标注", nav=1.0, nav_date="2026-08-11", ytd=10.0, one_year=12.0,
        volatility=None, drawdown=None, fee=fee, scale=None, inception=None, score=score,
        score_parts=[], reason="按 AKShare 当前公开排行字段排序；未获得的主数据不参与计算。",
        caveat="主数据待核验。", highlights=["单位净值已获取"], status="待核",
        source="AKShare public reference", snapshot="akshare-test-snapshot", chart=[], tags=["新能源"],
        intake=intake, quality_status="REFERENCE", nav_source_type="AKSHARE_PUBLIC",
        nav_trust_level="LOW", nav_freshness="REFERENCE",
    )


@pytest.fixture(autouse=True)
def reset_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    repository.funds = {
        fund.code: fund for fund in [
            reference_fund("008286", 82),
            reference_fund("012349", 78),
            reference_fund("012861", 69),
            reference_fund("004640", 57, risk="高风险", fee=1.5, intake="限额申购"),
            reference_fund("017327", 75, risk="中风险", intake="暂停申购"),
        ]
    }
    repository.histories = {}
    repository.watchlist = {}
    repository.runs = {}

    async def fake_research(query: str, funds: list[Fund], knowledge: list[object], limit: int) -> ModelResearchOutput:
        return ModelResearchOutput(
            intent=query, themes=["新能源"], ambiguities=[], summary="模型测试结论：仅基于候选快照进行比较。",
            ranking=[ModelAssessment(fundCode=fund.code, score=90 - index, fit="符合用户目标", reason="真实候选字段匹配。", riskFlags=[])
                     for index, fund in enumerate(funds[:limit])], followUpQuestions=[]
        )

    monkeypatch.setattr(llm_service, "research", fake_research)


client = TestClient(app)


def test_health_exposes_python_api_and_reference_mode():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "python-fastapi"
    assert body["mode"] == "reference"


def test_funds_are_compatible_with_vue_contract():
    response = client.get("/api/funds")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 5
    assert body["items"][0]["shortName"]
    assert body["items"][0]["navSourceType"] == "AKSHARE_PUBLIC"
    assert body["sourceType"] == "AKSHARE_PUBLIC"
    assert body["qualityStatus"] == "REFERENCE"


def test_screen_preserves_unknown_fields_for_reference_research():
    response = client.post("/api/funds/screen", json={
        "query": "新能源", "limit": 10,
        "filters": {"riskLevelMax": "中高风险", "maxFee": 1.2, "requireOpen": True},
    })
    assert response.status_code == 200
    codes = [item["code"] for item in response.json()["items"]]
    assert codes == ["008286", "012349", "012861"]


def test_recommendation_run_can_be_replayed():
    response = client.post("/api/recommendations/runs", json={
        "query": "长期定投新能源", "limit": 3,
        "filters": {"riskLevelMax": "中高风险", "maxFee": 1.2, "requireOpen": True},
    })
    assert response.status_code == 200
    run = response.json()
    trace = client.get(f"/api/recommendations/runs/{run['runId']}/trace")
    assert trace.status_code == 200
    assert trace.json()["snapshotId"] == run["snapshotId"]
    assert run["interpretation"]["provider"] == llm_service.provider
    assert run["mode"].endswith("LLM_RESEARCH")
    assert run["summary"]
    assert run["candidates"][0]["sourceSummary"]["officialDisclosureChecked"] is False


def test_session_llm_requires_a_key_and_does_not_fallback_to_global_rules():
    response = client.post("/api/recommendations/runs", json={
        "query": "短期测试", "limit": 1,
        "filters": {"maxFee": 1.2},
        "llm": {"provider": "openai-compatible", "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_LLM_CONFIGURATION"


def test_session_llm_key_is_not_written_to_research_run(monkeypatch: pytest.MonkeyPatch):
    async def fake_session_research(query: str, funds: list[Fund], knowledge: list[object], limit: int) -> ModelResearchOutput:
        return ModelResearchOutput(
            intent="会话模型测试", themes=[], ambiguities=[], summary="仅用于测试的会话模型输出。",
            ranking=[ModelAssessment(fundCode=funds[0].code, score=88, fit="测试匹配", reason="测试字段", riskFlags=[])],
            followUpQuestions=[],
        )

    class SessionService:
        provider = "openai-compatible"
        model = "session-model"

        async def research(self, query: str, funds: list[Fund], knowledge: list[object], limit: int) -> ModelResearchOutput:
            return await fake_session_research(query, funds, knowledge, limit)

    monkeypatch.setattr(llm_service, "for_session_request", lambda overrides: SessionService())
    response = client.post("/api/recommendations/runs", json={
        "query": "会话测试", "limit": 1, "filters": {},
        "llm": {"provider": "openai-compatible", "baseUrl": "https://api.deepseek.com/v1", "apiKey": "session-secret", "model": "deepseek-chat"},
    })
    assert response.status_code == 200
    assert "session-secret" not in response.text
    assert response.json()["interpretation"]["provider"] == "openai-compatible"


def test_watchlist_lifecycle():
    created = client.post("/api/watchlist/items", json={"fundCode": "008286", "reasonTags": ["新能源"]})
    assert created.status_code == 201
    assert client.get("/api/watchlist/items").json()["items"]
    assert client.delete("/api/watchlist/items/008286").status_code == 204


def test_compare_rejects_more_than_four_funds():
    response = client.post("/api/funds/compare", json={"codes": ["008286", "012349", "012861", "004640", "017327"]})
    assert response.status_code == 400


def test_history_endpoint_is_explicit_when_provider_is_unavailable():
    response = client.get("/api/funds/008286/history")
    assert response.status_code == 200
    body = response.json()
    assert body["fundCode"] == "008286"
    assert body["metricStatus"] == "UNAVAILABLE"
    assert body["items"] == []
    assert body["sourceType"] == "AKSHARE_PUBLIC"
