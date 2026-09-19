"""基金领域的 agent 工具集：把 repository / knowledge_base 包成 tau agent 可调用的工具。

工具设计原则（和 app/llm.py 的数据边界一致）：
- 工具只「读取」仓库里已有的真实数据，不编造、不补全缺失字段；
- 返回给模型的 JSON 与 LLMService._compact_fund 同构，模型引用证据的字段名保持一致；
- 每个工具执行时把「真实出现过的基金代码」记进 state["seen_codes"]，
  供最终 ranking 校验用 —— agent 自主检索的候选代码集合，替代旧链路的
  「预筛候选池」。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult
from tau_agent.types import JSONValue

CompactFundFn = Callable[[Any], dict[str, Any]]


def _json_result(data: Any) -> AgentToolResult:
    text = json.dumps(data, ensure_ascii=False)
    return AgentToolResult(content=[TextContent(text=text)], details={})


def _error_result(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=message)], details={})


def build_fund_tools(
    *,
    repository: Any,
    knowledge_base: Any,
    compact_fund: CompactFundFn,
) -> tuple[list[AgentTool], dict[str, Any]]:
    """构建基金研究领域工具。

    repository: DataRepository（含 n-gram 相关性索引）；
    knowledge_base: 研究知识库（含异步 search）；
    compact_fund: Fund -> 紧凑证据字典（与 LLMService._compact_fund 同一实现）。
    返回 (工具列表, state)；state["seen_codes"] 在执行中累积真实出现过的基金代码。
    """
    state: dict[str, Any] = {"seen_codes": set()}

    def _note(codes: list[str]) -> None:
        state["seen_codes"].update(codes)

    def _pack(fund: Any) -> dict[str, Any]:
        _note([fund.code])
        return compact_fund(fund)

    async def search_funds(
        _tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        _signal: object = None,
        _on_update: object = None,
    ) -> AgentToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return _error_result("query 不能为空")
        limit = min(max(int(arguments.get("limit", 10)), 1), 20)
        scores = repository.relevance_scores(query)
        if not scores:
            return _json_result({"matches": [], "note": "没有与该关键词相关的基金，可尝试换个说法。"})
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[: limit * 3]
        funds = [repository.get_fund(code) for code, _score in ranked]
        funds = [fund for fund in funds if fund is not None]
        matches = [_pack(fund) for fund in funds[:limit]]
        return _json_result({"totalRelevant": len(scores), "matches": matches})

    async def get_fund_detail(
        _tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        _signal: object = None,
        _on_update: object = None,
    ) -> AgentToolResult:
        code = str(arguments.get("code", "")).strip()
        fund = repository.get_fund(code) if code else None
        if fund is None:
            return _error_result(f"未找到基金代码 {code or '(空)'}，请先用 search_funds 确认代码。")
        return _json_result(_pack(fund))

    async def screen_funds(
        _tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        _signal: object = None,
        _on_update: object = None,
    ) -> AgentToolResult:
        universe = repository.list_funds()
        fund_type = str(arguments.get("fundType", "")).strip().lower()
        risk = arguments.get("maxRisk")
        min_one_year = arguments.get("minOneYear")
        max_drawdown = arguments.get("maxDrawdown")  # 正数，回撤绝对值上限
        min_scale = arguments.get("minScale")
        max_fee = arguments.get("maxFee")
        sort_by = str(arguments.get("sortBy", "oneYear")).strip()
        limit = min(max(int(arguments.get("limit", 10)), 1), 20)

        def _num(value: JSONValue) -> float | None:
            if value is None or value == "":
                return None
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        def _numeric(field_value: Any) -> float | None:
            return field_value if isinstance(field_value, (int, float)) else None

        passed: list[Any] = []
        for fund in universe:
            if fund_type and fund_type not in str(fund.type).lower():
                continue
            if risk is not None and str(fund.risk) != str(risk):
                continue
            one_year = _numeric(fund.one_year)
            if _num(min_one_year) is not None and (one_year is None or one_year < _num(min_one_year)):
                continue
            drawdown = _numeric(fund.drawdown)
            if _num(max_drawdown) is not None and (
                drawdown is None or abs(drawdown) > abs(_num(max_drawdown))
            ):
                continue
            scale = _numeric(fund.scale)
            if _num(min_scale) is not None and (scale is None or scale < _num(min_scale)):
                continue
            fee = _numeric(fund.fee)
            if _num(max_fee) is not None and (fee is None or fee > _num(max_fee)):
                continue
            passed.append(fund)

        sort_keys: dict[str, Callable[[Any], float]] = {
            "oneYear": lambda f: -(f.one_year if isinstance(f.one_year, (int, float)) else -1e18),
            "ytd": lambda f: -(f.ytd if isinstance(f.ytd, (int, float)) else -1e18),
            "drawdown": lambda f: abs(f.drawdown) if isinstance(f.drawdown, (int, float)) else 1e18,
            "scale": lambda f: -(f.scale if isinstance(f.scale, (int, float)) else -1e18),
            "fee": lambda f: f.fee if isinstance(f.fee, (int, float)) else 1e18,
        }
        passed.sort(key=sort_keys.get(sort_by, sort_keys["oneYear"]))
        matches = [_pack(fund) for fund in passed[:limit]]
        return _json_result({"totalPassed": len(passed), "matches": matches})

    async def search_knowledge(
        _tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        _signal: object = None,
        _on_update: object = None,
    ) -> AgentToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return _error_result("query 不能为空")
        limit = min(max(int(arguments.get("limit", 5)), 1), 8)
        chunks = await knowledge_base.search(query, limit=limit)
        items = [{"id": c.chunk_id, "title": c.title, "content": c.content} for c in chunks]
        return _json_result({"chunks": items})

    async def fund_history(
        _tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        _signal: object = None,
        _on_update: object = None,
    ) -> AgentToolResult:
        code = str(arguments.get("code", "")).strip()
        period = str(arguments.get("period", "1M")).strip() or "1M"
        if not code:
            return _error_result("code 不能为空")
        if repository.get_fund(code) is None:
            return _error_result(f"未找到基金代码 {code}，请先用 search_funds 确认代码。")
        try:
            records = await repository.history(code, period)
        except Exception as exc:  # noqa: BLE001 - 工具是隔离边界，报错回传给模型
            return _error_result(f"净值历史获取失败：{str(exc)[:200]}")
        _note([code])
        return _json_result({"code": code, "period": period, "points": len(records), "history": records})

    tools = [
        AgentTool(
            name="search_funds",
            label="搜索基金",
            description=(
                "按关键词（主题/名称/公司/经理等中文片段）在全部基金里搜索，"
                "返回按相关性排序的匹配列表。中文不需要分词，直接给主题词如「白酒」「红利」。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，例如「白酒」"},
                    "limit": {"type": "integer", "description": "返回数量，默认 10，最多 20"},
                },
                "required": ["query"],
            },
            execute_fn=search_funds,
        ),
        AgentTool(
            name="screen_funds",
            label="筛选基金",
            description=(
                "按量化条件筛选全部基金：类型、风险等级、最低近一年收益、回撤上限、"
                "最低规模、费率上限，并可按收益/回撤/规模/费率排序。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fundType": {"type": "string", "description": "类型关键词，如「混合」「指数」"},
                    "maxRisk": {"type": "integer", "description": "最高风险等级 1-5"},
                    "minOneYear": {"type": "number", "description": "近一年收益率下限（百分数）"},
                    "maxDrawdown": {"type": "number", "description": "回撤绝对值上限（正数），如 20 表示回撤不超过 -20%"},
                    "minScale": {"type": "number", "description": "规模下限（亿元）"},
                    "maxFee": {"type": "number", "description": "费率上限（百分数）"},
                    "sortBy": {"type": "string", "enum": ["oneYear", "ytd", "drawdown", "scale", "fee"]},
                    "limit": {"type": "integer", "description": "返回数量，默认 10，最多 20"},
                },
            },
            execute_fn=screen_funds,
        ),
        AgentTool(
            name="get_fund_detail",
            label="基金详情",
            description="按 6 位基金代码取单只基金的完整字段（经理/主题/回撤/波动率/规模/费率等）。",
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string", "description": "6 位基金代码"}},
                "required": ["code"],
            },
            execute_fn=get_fund_detail,
        ),
        AgentTool(
            name="fund_history",
            label="净值历史",
            description="按代码与周期（如 1M/3M/6M/1Y）取净值走势记录，用于看波动与回撤。",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位基金代码"},
                    "period": {"type": "string", "description": "周期，默认 1M"},
                },
                "required": ["code"],
            },
            execute_fn=fund_history,
        ),
        AgentTool(
            name="search_knowledge",
            label="检索知识库",
            description="检索基金研究知识库（指标口径、风险提示等），用于核对术语与数据边界。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，如「最大回撤」「夏普比率」"},
                    "limit": {"type": "integer", "description": "返回片段数，默认 5"},
                },
                "required": ["query"],
            },
            execute_fn=search_knowledge,
        ),
    ]
    return tools, state
