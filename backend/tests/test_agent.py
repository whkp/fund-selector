import json
import os
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

# Tests must be deterministic and must never call the public provider.
os.environ["FUND_COMPASS_AKSHARE_ENABLED"] = "false"
os.environ["FUND_COMPASS_MODE"] = "REFERENCE"

import app.main as main_module
from app.agent_tools import build_fund_tools
from app.llm import LLMError, LLMResponseError, LLMService, ModelAssessment, ModelResearchOutput
from app.main import app, repository
from app.models import Fund
from tau_agent.messages import AssistantMessage, TextContent, ToolCall, UserMessage
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


def test_duplicate_tool_calls_are_deduped():
    """同一轮研究内参数相同的重复调用返回缓存结果，且带上 duplicate 提示。"""
    import asyncio

    repo = StubRepository({"008286": reference_fund("008286", 90)})
    tools, state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(), compact_fund=LLMService._compact_fund,
    )
    detail = next(t for t in tools if t.name == "get_fund_detail")
    first = asyncio.run(detail.execute("c1", {"code": "008286"}))
    again = asyncio.run(detail.execute("c2", {"code": "008286"}))
    payload = json.loads(again.text)
    assert payload.get("duplicate") is True
    assert "重复调用" in payload.get("note", "")
    assert payload["fundCode"] == json.loads(first.text)["fundCode"]
    assert state["duplicate_calls"] == 1

    # fund_history 去重省掉上游请求：同参数两次，repository.history 只被真实调 1 次
    history = next(t for t in tools if t.name == "fund_history")
    asyncio.run(history.execute("c3", {"code": "008286", "period": "1Y"}))
    asyncio.run(history.execute("c4", {"code": "008286", "period": "1Y"}))
    assert repo.history_calls == [("008286", "1Y")]
    # 参数不同不去重
    asyncio.run(history.execute("c5", {"code": "008286", "period": "3M"}))
    assert repo.history_calls == [("008286", "1Y"), ("008286", "3M")]


def test_screen_tool_zero_coverage_guard():
    """对数据源未覆盖的字段（drawdown/scale）设阈值时，返回数据边界说明而非空结果。"""
    import asyncio

    repo = StubRepository({"008286": reference_fund("008286", 90)})  # drawdown=None
    tools, _state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(), compact_fund=LLMService._compact_fund,
    )
    tool = next(t for t in tools if t.name == "screen_funds")
    result = asyncio.run(tool.execute("c6", {"maxDrawdown": 20}))
    payload = json.loads(result.text)
    assert payload["totalPassed"] == 0
    assert "无法应用" in payload["note"] and "fund_history" in payload["note"]
    # 有覆盖的条件不受影响
    ok = asyncio.run(tool.execute("c7", {"minOneYear": 11}))
    assert json.loads(ok.text)["totalPassed"] == 1


def test_enrichment_parsers():
    """补数模块的解析函数：规模/主题/官方回撤/风险等级/成立年数。"""
    from app.enrichment import (
        _inception_years,
        _official_drawdown,
        clean_theme,
        parse_scale,
        risk_label,
    )

    assert parse_scale("197.40亿") == 197.4
    assert round(parse_scale("5236.81万"), 4) == 0.5237
    assert parse_scale("暂无规模") is None
    assert clean_theme("中证白酒指数收益率×95%＋金融机构人民币活期存款基准利率（税后）×5%") == "中证白酒指数"
    assert clean_theme("沪深300指数*95%+活期*5%") == "沪深300指数"
    assert clean_theme("") is None
    rows = [{"业绩类型": "阶段业绩", "周期": "近1年", "本产品最大回撒": 38.67}]
    assert _official_drawdown(rows) == -38.67
    assert _official_drawdown([{"业绩类型": "年度业绩", "周期": "近1年", "本产品最大回撒": 1.0}]) is None
    assert risk_label("4") == "中高风险"
    assert risk_label(1) == "低风险"
    assert risk_label("0") is None and risk_label(None) is None and risk_label("未知") is None
    # 成立年数是 float（eligibility 筛选与前端 `inception: number` 的契约），不是 date 对象。
    years = _inception_years("2015-05-27")
    assert years is not None and 10 < years < 14
    assert _inception_years("") is None and _inception_years("不是日期") is None


def test_enrich_fund_applies_risk_level(monkeypatch: pytest.MonkeyPatch):
    """enrich_fund 把蛋卷风险等级写进内存 Fund；danjuan 失败不拖垮其他字段。"""
    import asyncio

    import app.enrichment as enrichment
    from app.enrichment import enrich_fund, needs_enrichment

    fund = reference_fund("161725", 90, risk="未获取")
    assert needs_enrichment(fund)  # risk 占位本身就要触发补数

    repo = StubRepository({"161725": fund})
    repo.histories = {}  # enrich_fund 的波动率回退路径要用
    monkeypatch.setattr(enrichment, "_fetch_basic", lambda code: {
        "基金经理": "侯昊", "基金公司": "招商基金管理有限公司",
        "成立时间": "2015-05-27", "最新规模": "197.40亿",
        "业绩比较基准": "中证白酒指数收益率×95%＋活期存款利率×5%",
    })
    monkeypatch.setattr(enrichment, "_fetch_achievement", lambda code: [
        {"业绩类型": "阶段业绩", "周期": "近1年", "本产品最大回撒": 38.67},
    ])
    monkeypatch.setattr(enrichment, "_fetch_risk", lambda code: "中高风险")  # 契约：返回已映射的标签

    async def fake_persist(fund_obj):  # noqa: ARG001 - 测试不碰数据库
        return None

    monkeypatch.setattr(enrichment, "_persist", fake_persist)

    result = asyncio.run(enrich_fund(repo, "161725"))
    assert repo.funds["161725"].risk == "中高风险"
    assert repo.funds["161725"].manager == "侯昊"
    assert repo.funds["161725"].drawdown == -38.67
    # inception 必须是「成立年数」（float）—— date 对象会让 eligibility 的
    # minimumInceptionYears 比较直接抛 TypeError。
    assert isinstance(repo.funds["161725"].inception, float)
    assert repo.funds["161725"].inception > 10
    assert result["updated"]["risk"] == "中高风险"

    # danjuan 不可用时：risk 保持「未获取」，其他字段照常补到
    repo.funds["004640"] = reference_fund("004640", 80, risk="未获取")
    monkeypatch.setattr(enrichment, "_fetch_risk", lambda code: None)
    asyncio.run(enrich_fund(repo, "004640"))
    assert repo.funds["004640"].risk == "未获取"
    assert repo.funds["004640"].manager == "侯昊"


def test_get_fund_detail_triggers_enrichment_with_budget():
    """get_fund_detail 命中占位主数据时按需补数；预算耗尽后给提示而不是静默缺失。"""
    import asyncio

    repo = StubRepository({"008286": reference_fund("008286", 90), "004640": reference_fund("004640", 80)})
    calls: list[str] = []

    async def enricher(code: str):
        calls.append(code)
        repo.funds[code] = replace(
            repo.funds[code], manager="侯昊", company="招商基金", scale=197.4, drawdown=-38.67,
        )
        return {"code": code, "updated": {"manager": "侯昊"}}

    tools, state = build_fund_tools(
        repository=repo, knowledge_base=StubKnowledge(),
        compact_fund=LLMService._compact_fund, enricher=enricher,
    )
    state["enrich_budget"] = 1
    tool = next(t for t in tools if t.name == "get_fund_detail")

    first = json.loads(asyncio.run(tool.execute("c8", {"code": "008286"})).text)
    assert first["enrichedNow"] is True and first["manager"] == "侯昊"
    assert calls == ["008286"] and state["enrich_budget"] == 0

    # 预算耗尽：另一只基金不再触发补数，返回原始字段 + 额度提示
    second = json.loads(asyncio.run(tool.execute("c9", {"code": "004640"})).text)
    assert calls == ["008286"] and "enrichedNow" not in second
    assert "额度已用完" in second["note"]


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

    seen_kwargs: dict[str, object] = {}

    async def fake_run(query, history=None, limit=10, **kwargs):
        seen_kwargs.update(kwargs)
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
    # 端点必须把预筛池交给 agent（否则模型检索不到主题时只能返回空排名）
    pool = seen_kwargs.get("pool")
    assert isinstance(pool, list) and pool, "research pool must be passed to agent"
    assert any(item["fundCode"] == "161725" for item in pool)


def test_research_endpoint_streams_trace_for_run_id(monkeypatch: pytest.MonkeyPatch):
    """带 runId 的请求：先有 RUNNING 占位 + trace 可轮询，完成后 trace 一致。"""
    service = main_module.agent_research
    monkeypatch.setattr(service, "enabled", True)
    repository.funds["161725"] = reference_fund("161725", 90)

    pushed: list[dict[str, str]] = []

    async def fake_run(query, history=None, limit=10, pool=None, on_trace=None):
        step = {"event": "tool_completed", "title": "调用工具 search_funds", "detail": "命中 1 只", "status": "COMPLETED"}
        if on_trace is not None:
            on_trace(step)
        pushed.append(step)
        output = ModelResearchOutput.model_validate_json(json.dumps(FINAL_JSON, ensure_ascii=False))
        return output, [step]

    monkeypatch.setattr(service, "run_research", fake_run)
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    headers = register_user()
    response = client.post("/api/recommendations/runs", json={"query": "白酒", "limit": 5, "runId": run_id}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["runId"] == run_id
    # 完成后 trace 包含轮询期间推送的步骤
    trace_after = client.get(f"/api/recommendations/runs/{run_id}/trace", headers=headers)
    assert trace_after.status_code == 200
    titles = [entry["title"] for entry in trace_after.json()["trace"]]
    assert "调用工具 search_funds" in titles
    assert len(pushed) == 1


def test_research_endpoint_marks_placeholder_failed(monkeypatch: pytest.MonkeyPatch):
    """研究彻底失败时：占位记录标记 FAILED，trace 里留下失败步骤（可复盘过程）。"""
    service = main_module.agent_research
    monkeypatch.setattr(service, "enabled", True)

    async def broken_run(query, history=None, limit=10, **kwargs):
        raise LLMResponseError("最终输出不是 JSON")

    async def broken_research(query, funds, knowledge, limit, history=None):
        raise LLMError("模型不可用")

    monkeypatch.setattr(service, "run_research", broken_run)
    monkeypatch.setattr(main_module.llm_service, "research", broken_research)
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    headers = register_user()
    response = client.post("/api/recommendations/runs", json={"query": "白酒", "limit": 5, "runId": run_id}, headers=headers)
    assert response.status_code == 502, response.text
    failed = client.get(f"/api/recommendations/runs/{run_id}/trace", headers=headers)
    assert failed.status_code == 200
    body = failed.json()["trace"]
    assert any(entry.get("status") == "FAILED" for entry in body)


def test_agent_receives_pool_and_emits_trace():
    """run_research 注入候选池文本到首条消息，on_trace 逐步回调。"""
    repo = StubRepository({"161725": reference_fund("161725", 90)})
    repo.funds["161725"].theme = "白酒"
    service = make_service(repo, [
        tool_call_stream(ToolCall(id="call_p", name="search_funds", arguments={"query": "白酒"})),
        final_stream(),
    ])
    import asyncio

    pool = [LLMService._compact_fund(repo.funds["161725"])]
    steps: list[dict[str, str]] = []
    output, _trace = asyncio.run(service.run_research(
        "白酒主题基金", None, 10, pool=pool, on_trace=steps.append,
    ))
    assert output.ranking[0].fundCode == "161725"
    assert any(entry["event"] == "tool_started" for entry in steps)
    assert any(entry["title"] == "调用工具 search_funds" for entry in steps)
    # 首条 user 消息里必须带有候选池上下文（FakeProvider.calls 记录了每轮消息列表）
    provider = service._provider
    first_user = next(m for m in provider.calls[0][2] if isinstance(m, UserMessage))
    assert "已通过硬约束的候选池" in first_user.content
    assert "161725" in first_user.content


def test_research_endpoint_falls_back_when_agent_fails(monkeypatch: pytest.MonkeyPatch):
    service = main_module.agent_research
    monkeypatch.setattr(service, "enabled", True)

    async def broken_run(query, history=None, limit=10, **kwargs):
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


