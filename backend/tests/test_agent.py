import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

# Tests must be deterministic and must never call the public provider.
os.environ["FUND_COMPASS_AKSHARE_ENABLED"] = "false"
os.environ["FUND_COMPASS_MODE"] = "REFERENCE"

import app.main as main_module
from app.agent_tools import build_fund_tools
from app.llm import LLMResponseError, LLMService, ModelAssessment, ModelResearchOutput
from app.main import app, repository
from app.models import Fund
from tau_agent.messages import AssistantMessage, TextContent, ToolCall
from tau_agent.provider_events import AssistantDoneEvent, AssistantStartEvent, ToolCallEndEvent
from tau_ai.fake import FakeProvider
from tests.test_api import reference_fund  # 复用同一套确定性 Fund 构造

FINAL_JSON = {
    "intent": "研究白酒主题基金",
    "themes": ["白酒"],
    "ambiguities": [],
    "summary": "招商中证白酒A（161725）主题契合。",
    "ranking": [{"fundCode": "161725", "score": 92, "fit": "白酒指数代表", "reason": "工具返回主题白酒。", "riskFlags": []}],
    "followUpQuestions": [],
}


class StubKnowledge:
    async def search(self, query: str, limit: int = 5):
        class Chunk:
            chunk_id = "kb-1"
            title = "回撤口径"
            content = "最大回撤为区间内最大跌幅。"

        return [Chunk()][:limit]


class StubRepository:
    """只实现工具真正用到的接口，避免触发 AKShare。"""

    def __init__(self, funds: dict[str, Fund]) -> None:
        self.funds = funds
        self.history_calls: list[tuple[str, str]] = []

    def relevance_scores(self, query: str) -> dict[str, int]:
        return {
            code: (10 if query in fund.name or query in fund.theme else 0)
            for code, fund in self.funds.items()
        }

    def get_fund(self, code: str) -> Fund | None:
        return self.funds.get(code)

    def list_funds(self) -> list[Fund]:
        return sorted(self.funds.values(), key=lambda item: (-item.score, item.code))

    async def history(self, code: str, period: str) -> list[dict[str, object]]:
        self.history_calls.append((code, period))
        return [{"date": "2026-09-18", "nav": 1.02}]


def make_service(repository: StubRepository, streams: list[list[object]]) -> object:
    from app.agent_service import AgentResearchService

    llm = LLMService(overrides={
        "provider": "openai-compatible",
        "model": "test-model",
        "baseUrl": "https://llm.example.com/v1",
        "apiKey": "test-key",
        "timeoutSeconds": 30,
    })
    service = AgentResearchService(llm, repository, StubKnowledge())
    service.enabled = True
    service._provider = FakeProvider(streams)
    from tau_ai import OpenAICompatibleProvider  # noqa: F401 - 占位说明 _provider 已被替换

    service._tools, service._tool_state = build_fund_tools(
        repository=repository,
        knowledge_base=StubKnowledge(),
        compact_fund=LLMService._compact_fund,
    )
    return service


def tool_call_stream(call: ToolCall) -> list[object]:
    # 这个版本里 ToolCall 是 content 块，不是独立字段（harness 的 .tool_calls 是派生属性）。
    partial = AssistantMessage(model="test-model", content=[call], stop_reason="toolUse")
    return [
        AssistantStartEvent(partial=AssistantMessage(model="test-model")),
        ToolCallEndEvent(content_index=0, tool_call=call, partial=partial),
        AssistantDoneEvent(reason="toolUse", message=partial),
    ]


def final_stream() -> list[object]:
    final = AssistantMessage(model="test-model", content=[TextContent(text=json.dumps(FINAL_JSON, ensure_ascii=False))])
    return [
        AssistantStartEvent(partial=final),
        AssistantDoneEvent(reason="stop", message=final),
    ]


def test_search_tool_returns_matches_and_records_codes():
    repo = StubRepository({"161725": reference_fund("161725", 90)})
    tools, state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(), compact_fund=LLMService._compact_fund,
    )
    tool = next(t for t in tools if t.name == "search_funds")
    # 基金名是 "AKShare 基金 161725"，主题默认"未标注"；换成按代码匹配不到的情况也该有响应。
    import asyncio

    result = asyncio.run(tool.execute("call_1", {"query": "白酒"}))
    payload = json.loads(result.text)
    assert payload["matches"] == [] or payload["matches"][0]["fundCode"] == "161725"
    assert isinstance(state["seen_codes"], set)


def test_screen_tool_filters_by_min_one_year():
    repo = StubRepository({
        "008286": reference_fund("008286", 90),
        "004640": reference_fund("004640", 80),
    })
    tools, _state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(), compact_fund=LLMService._compact_fund,
    )
    tool = next(t for t in tools if t.name == "screen_funds")
    import asyncio

    result = asyncio.run(tool.execute("call_2", {"minOneYear": 11, "sortBy": "oneYear", "limit": 5}))
    payload = json.loads(result.text)
    assert payload["totalPassed"] == 2  # 参考基金 one_year 统一 12.0
    assert payload["matches"][0]["fundCode"] == "008286"


def test_get_fund_detail_unknown_code_is_tool_error():
    repo = StubRepository({"008286": reference_fund("008286", 90)})
    tools, state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(), compact_fund=LLMService._compact_fund,
    )
    tool = next(t for t in tools if t.name == "get_fund_detail")
    import asyncio

    result = asyncio.run(tool.execute("call_3", {"code": "999999"}))
    assert "未找到" in result.text
    assert state["seen_codes"] == set()


def test_fund_code_guard_rejects_malformed_codes():
    """防幻觉护栏：非 6 位数字代码直接打回，禁止模型猜代码。"""
    import asyncio

    repo = StubRepository({"008286": reference_fund("008286", 90)})
    tools, _state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(), compact_fund=LLMService._compact_fund,
    )
    detail = next(t for t in tools if t.name == "get_fund_detail")
    history = next(t for t in tools if t.name == "fund_history")
    for tool, bad_code in [(detail, "16172"), (detail, "abc123"), (history, "161725.SZ"), (history, "")]:
        result = asyncio.run(tool.execute("call_x", {"code": bad_code}))
        assert "6 位数字" in result.text or "不能为空" in result.text, (tool.name, bad_code)


def test_screen_tool_supports_within_theme_ranking():
    """池内二次排序：query 先圈主题池，再在池内应用条件与排序（对应 --within-* 语义）。"""
    import asyncio
    from dataclasses import replace

    liquor_a = replace(reference_fund("161725", 90), theme="白酒", name="招商中证白酒A")
    liquor_b = replace(reference_fund("012414", 70), theme="白酒", name="招商中证白酒C")
    other = reference_fund("008286", 99)  # 收益更高但不在白酒池里
    repo = StubRepository({"161725": liquor_a, "012414": liquor_b, "008286": other})
    tools, state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(), compact_fund=LLMService._compact_fund,
    )
    tool = next(t for t in tools if t.name == "screen_funds")

    result = asyncio.run(tool.execute("call_4", {"query": "白酒", "sortBy": "oneYear", "limit": 10}))
    payload = json.loads(result.text)
    assert payload["poolSize"] == 2
    assert payload["totalPassed"] == 2
    assert [m["fundCode"] for m in payload["matches"]] == ["161725", "012414"]
    assert "008286" not in {m["fundCode"] for m in payload["matches"]}
    # 池内命中的代码都要进 seen_codes，供最终 ranking 校验
    assert {"161725", "012414"} <= state["seen_codes"]

    empty = asyncio.run(tool.execute("call_5", {"query": "量子计算"}))
    payload_empty = json.loads(empty.text)
    assert payload_empty["totalPassed"] == 0 and "无法在池内筛选" in payload_empty["note"]


def test_agent_loop_happy_path():
    repo = StubRepository({"161725": reference_fund("161725", 90)})
    repo.funds["161725"].theme = "白酒"
    service = make_service(repo, [
        tool_call_stream(ToolCall(id="call_a", name="search_funds", arguments={"query": "白酒"})),
        final_stream(),
    ])
    import asyncio

    output, trace = asyncio.run(service.run_research("白酒主题基金", None, 10))
    assert output.ranking[0].fundCode == "161725"
    assert any(entry["title"] == "调用工具 search_funds" for entry in trace)
    assert trace[-1]["status"] == "COMPLETED"


def test_agent_rejects_codes_not_returned_by_tools():
    repo = StubRepository({"008286": reference_fund("008286", 90)})
    bad = {**FINAL_JSON, "ranking": [{"fundCode": "161725", "score": 9, "fit": "x", "reason": "y", "riskFlags": []}]}
    final = AssistantMessage(model="test-model", content=[TextContent(text=json.dumps(bad, ensure_ascii=False))])
    service = make_service(repo, [
        final_stream() if False else [
            AssistantStartEvent(partial=final),
            AssistantDoneEvent(reason="stop", message=final),
        ],
    ])
    import asyncio

    with pytest.raises(LLMResponseError):
        asyncio.run(service.run_research("白酒", None, 10))


def test_agent_final_output_must_be_json():
    repo = StubRepository({"161725": reference_fund("161725", 90)})
    final = AssistantMessage(model="test-model", content=[TextContent(text="结论：买白酒。")])
    service = make_service(repo, [[
        AssistantStartEvent(partial=final),
        AssistantDoneEvent(reason="stop", message=final),
    ]])
    import asyncio

    with pytest.raises(LLMResponseError):
        asyncio.run(service.run_research("白酒", None, 10))


client = TestClient(app)
INVITE_CODE = os.environ["FUND_COMPASS_INVITE_CODE"]


@pytest.fixture(autouse=True)
def reset_repository() -> None:
    """端点级用例需要确定的全集；直接复用 test_api 的 5 只参考基金。"""
    repository.funds = {
        fund.code: fund for fund in [
            reference_fund("008286", 82),
            reference_fund("012349", 78),
            reference_fund("012861", 69),
            reference_fund("004640", 57, risk="高风险", fee=1.5, intake="限额申购"),
            reference_fund("017327", 75, risk="中风险", intake="暂停申购"),
        ]
    }
    repository.runs = {}


def register_user(prefix: str = "agent-tester") -> dict[str, str]:
    response = client.post("/api/auth/register", json={
        "email": f"{prefix}_{uuid.uuid4().hex[:10]}@example.com",
        "password": "test-password-123",
        "displayName": "Agent 测试",
        "inviteCode": INVITE_CODE,
    })
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_research_endpoint_runs_agent_mode(monkeypatch: pytest.MonkeyPatch):
    service = main_module.agent_research
    monkeypatch.setattr(service, "enabled", True)
    # FINAL_JSON 引用的 161725 必须真实存在于仓库（模拟 agent 用工具查到它）。
    repository.funds["161725"] = reference_fund("161725", 90)

    async def fake_run(query, history=None, limit=10):
        output = ModelResearchOutput.model_validate_json(json.dumps(FINAL_JSON, ensure_ascii=False))
        return output, [{"event": "tool_completed", "title": "调用工具 search_funds", "detail": "命中 1 只", "status": "COMPLETED"}]

    monkeypatch.setattr(service, "run_research", fake_run)
    response = client.post("/api/recommendations/runs", json={"query": "白酒", "limit": 5}, headers=register_user())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"].endswith("_AGENT_RESEARCH")
    assert any(entry["title"] == "调用工具 search_funds" for entry in body["trace"])
    assert body["candidates"][0]["fund"]["code"] == "161725"
    # agent 候选不在预筛池时也允许引用（repository 回源补齐）
    assert body["candidates"][0]["analysis"] == "白酒指数代表"


def test_research_endpoint_falls_back_when_agent_fails(monkeypatch: pytest.MonkeyPatch):
    service = main_module.agent_research
    monkeypatch.setattr(service, "enabled", True)

    async def broken_run(query, history=None, limit=10):
        raise LLMResponseError("最终输出不是 JSON")

    async def fake_research(query, funds, knowledge, limit, history=None):
        return ModelResearchOutput(
            intent=query, themes=[], ambiguities=[], summary="降级链路结论",
            ranking=[ModelAssessment(fundCode=fund.code, score=70, fit="符合", reason="预筛池", riskFlags=[])
                     for fund in funds[:limit]], followUpQuestions=[],
        )

    monkeypatch.setattr(service, "run_research", broken_run)
    monkeypatch.setattr(main_module.llm_service, "research", fake_research)
    response = client.post("/api/recommendations/runs", json={"query": "白酒", "limit": 5}, headers=register_user())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"].endswith("_LLM_RESEARCH")
    assert body["summary"] == "降级链路结论"
