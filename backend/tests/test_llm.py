import asyncio

import httpx
import pytest

from app.llm import LLMConfigurationError, LLMResponseError, LLMService
from app.models import Fund


def fund() -> Fund:
    return Fund(
        id="ak-000001", code="000001", name="测试基金", short_name="测试基金", type="混合型", risk="未获取",
        manager="未获取", manager_years=None, company="未获取", theme="未标注", nav=1.0, nav_date="2026-08-11",
        ytd=10.0, one_year=12.0, volatility=None, drawdown=None, fee=0.15, scale=None, inception=None,
        score=70, score_parts=[], reason="参考字段", caveat="待核验", highlights=[], status="待核",
        source="AKShare public reference", snapshot="test", chart=[], tags=[], intake="未获取",
        quality_status="REFERENCE",
    )


def test_openai_compatible_research_uses_real_candidate_codes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("FUND_COMPASS_LLM_MODEL", "test-model")
    monkeypatch.setenv("FUND_COMPASS_LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("FUND_COMPASS_LLM_API_KEY", "test-key")
    request_data = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            request_data["url"] = url
            request_data["headers"] = kwargs["headers"]
            request_data["payload"] = kwargs["json"]
            return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": """{
              \"intent\": \"长期研究\", \"themes\": [\"成长\"], \"ambiguities\": [],
              \"summary\": \"仅按候选快照比较。\", \"ranking\": [{
                \"fundCode\": \"000001\", \"score\": 82, \"fit\": \"匹配\", \"reason\": \"费用字段可用。\", \"riskFlags\": [\"字段待核验\"]
              }], \"followUpQuestions\": []
            }"""}}]})

    monkeypatch.setattr("app.llm.httpx.AsyncClient", FakeClient)
    output = asyncio.run(LLMService().research("长期", [fund()], [], 3))
    assert output.ranking[0].fundCode == "000001"
    assert request_data["url"] == "https://llm.example/v1/chat/completions"
    assert request_data["headers"]["Authorization"] == "Bearer test-key"
    assert request_data["payload"]["response_format"]["type"] == "json_object"


def test_llm_rejects_codes_outside_the_actual_candidate_pool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("FUND_COMPASS_LLM_MODEL", "test-model")
    monkeypatch.setenv("FUND_COMPASS_LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("FUND_COMPASS_LLM_API_KEY", "test-key")

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": """{
              \"intent\": \"测试\", \"summary\": \"测试\", \"ranking\": [{
                \"fundCode\": \"999999\", \"score\": 50, \"fit\": \"测试\", \"reason\": \"测试\"
              }]}
            """}}]})

    monkeypatch.setattr("app.llm.httpx.AsyncClient", FakeClient)
    with pytest.raises(LLMResponseError, match="候选列表之外"):
        asyncio.run(LLMService().research("测试", [fund()], [], 3))


def test_session_endpoint_rejects_untrusted_or_plaintext_hosts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_LLM_ALLOWED_HOSTS", "api.deepseek.com")
    with pytest.raises(LLMConfigurationError, match="白名单"):
        LLMService({"provider": "openai-compatible", "baseUrl": "https://evil.example/v1", "apiKey": "x", "model": "m"}).validate_session_endpoint()
    with pytest.raises(LLMConfigurationError, match="HTTPS"):
        LLMService({"provider": "openai-compatible", "baseUrl": "http://api.deepseek.com/v1", "apiKey": "x", "model": "m"}).validate_session_endpoint()
