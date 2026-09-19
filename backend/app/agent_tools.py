"""基金领域的 agent 工具集：把 repository / knowledge_base 包成 tau agent 可调用的工具。

工具设计原则（和 app/llm.py 的数据边界一致）：
- 工具只「读取」仓库里已有的真实数据，不编造、不补全缺失字段；
- 返回给模型的 JSON 与 LLMService._compact_fund 同构，模型引用证据的字段名保持一致；
- 每个工具执行时把「真实出现过的基金代码」记进 state["seen_codes"]，
  供最终 ranking 校验用 —— agent 自主检索的候选代码集合，替代旧链路的
  「预筛候选池」。
"""

from __future__ import annotations

import dataclasses
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


def _dedup_wrapper(name: str, fn: Any, state: dict[str, Any]) -> Any:
    """同一轮研究内，参数完全相同的重复调用直接返回缓存结果。

    实测 qwen3.8-flash 会连续用相同参数调 search_knowledge（E2E 里 3 次）；
    对 fund_history 这类上游接口调用，去重同时省掉一次 AKShare 请求。
    缓存结果带上 duplicate 标记提示模型别再重复，行为对 harness 透明。
    """

    async def wrapped(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        cache: dict[str, AgentToolResult] = state.setdefault("call_cache", {})
        key = json.dumps([name, dict(arguments)], ensure_ascii=False, sort_keys=True, default=str)
        if key in cache:
            state["duplicate_calls"] = state.get("duplicate_calls", 0) + 1
            state["last_call_duplicate"] = True
            cached_text = cache[key].text
            note = "本工具刚以完全相同的参数调用过，以上为缓存结果；请直接使用已有信息继续分析，不要再重复调用。"
            try:
                payload = json.loads(cached_text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                payload["duplicate"] = True
                existing_note = payload.get("note")
                payload["note"] = f"{existing_note}；{note}" if existing_note else note
                return AgentToolResult(
                    content=[TextContent(text=json.dumps(payload, ensure_ascii=False))], details={}
                )
            return AgentToolResult(content=[TextContent(text=f"{cached_text}\n[{note}]")], details={})

        state["last_call_duplicate"] = False
        result = await fn(tool_call_id, arguments, signal, on_update)
        cache[key] = result
        return result

    return wrapped


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

    _SIX_DIGITS = set("0123456789")

    def _check_code(code: str) -> str | None:
        """防幻觉护栏：基金代码必须是 6 位数字，禁止模型猜代码。"""
        if not code:
            return "code 不能为空"
        if len(code) != 6 or not set(code) <= _SIX_DIGITS:
            return f"基金代码必须是 6 位数字，收到「{code}」不合法；禁止猜测代码，请先用 search_funds 查询。"
        return None

    async def get_fund_detail(
        _tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        _signal: object = None,
        _on_update: object = None,
    ) -> AgentToolResult:
        code = str(arguments.get("code", "")).strip()
        invalid = _check_code(code)
        if invalid:
            return _error_result(invalid)
        fund = repository.get_fund(code)
        if fund is None:
            return _error_result(f"未找到基金代码 {code}，请先用 search_funds 确认代码。")
        return _json_result(_pack(fund))

    async def screen_funds(
        _tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        _signal: object = None,
        _on_update: object = None,
    ) -> AgentToolResult:
        universe = repository.list_funds()
        query = str(arguments.get("query", "")).strip()
        if query:
            # 池内筛选（对应专业选股工具的 within 语义）：先用相关性索引圈主题池，
            # 再在池内应用量化条件与排序 ——「白酒里回撤最小」一类复合问题一步到位。
            scores = repository.relevance_scores(query)
            allowed = {code for code, score in (scores or {}).items() if score > 0}
            universe = [fund for fund in universe if fund.code in allowed]
            if not universe:
                return _json_result({
                    "totalPassed": 0,
                    "matches": [],
                    "note": f"没有与「{query}」主题相关的基金，无法在池内筛选；可先用 search_funds 换关键词确认。",
                })
        fund_type = str(arguments.get("fundType", "")).strip().lower()
        risk = arguments.get("maxRisk")
        min_one_year = arguments.get("minOneYear")
        max_drawdown = arguments.get("maxDrawdown")  # 正数，回撤绝对值上限
        min_scale = arguments.get("minScale")
        max_fee = arguments.get("maxFee")
        sort_by = str(arguments.get("sortBy", "oneYear")).strip()
        limit = min(max(int(arguments.get("limit", 10)), 1), 20)

        # 零覆盖守卫：数据源目前不产出 drawdown/scale 等字段，若模型对全空字段
        # 设阈值必然得到 0 结果且误以为「没有匹配」。提前给出数据边界说明。
        _CONDITION_FIELDS = {
            "maxDrawdown": "drawdown",
            "minScale": "scale",
            "minOneYear": "one_year",
            "maxFee": "fee",
        }

        def _numeric(field_value: Any) -> float | None:
            return field_value if isinstance(field_value, (int, float)) else None

        for arg_name, field_name in _CONDITION_FIELDS.items():
            if arguments.get(arg_name) is None:
                continue
            if not any(_numeric(getattr(fund, field_name, None)) is not None for fund in universe):
                return _json_result({
                    "totalPassed": 0,
                    "matches": [],
                    "note": (
                        f"条件 {arg_name} 无法应用：当前数据源未覆盖 {field_name} 字段"
                        f"（全池均为空）。不要据此认为没有匹配；需要该指标时用 fund_history "
                        f"取净值后自行计算，或改用其他可用条件（oneYear/ytd/fee/type）。"
                    ),
                })

        def _num(value: JSONValue) -> float | None:
            if value is None or value == "":
                return None
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

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
        payload: dict[str, Any] = {"totalPassed": len(passed), "matches": matches}
        if query:
            payload["poolSize"] = len(universe)
            payload["note"] = f"先按「{query}」圈定 {len(universe)} 只主题池，再应用条件筛选。"
        return _json_result(payload)

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
        invalid = _check_code(code)
        if invalid:
            return _error_result(invalid)
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
                "用于主题发现；找到候选代码后再用 get_fund_detail 精查单只。"
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
                "按量化条件在基金全集（或指定主题池内）筛选并排序。"
                "工具选择规则：主题契合看 query，收益/回撤/规模/费率条件看阈值参数，"
                "「某主题里 XX 最优」的复合问题用 query + sortBy 一步完成，不要先搜再手工过滤。"
                "参数口径：minOneYear/maxDrawdown/minScale/maxFee 均为百分数或亿元数值，"
                "是筛选阈值；limit 只是返回条数上限，不能当阈值用。"
                "带阈值条件时字段缺失（null）的基金会被剔除，这是数据边界，不要靠放宽条件硬凑。"
                "当前数据源字段可用性：oneYear/ytd/fee/type/nav 有值；"
                "drawdown/scale/volatility/risk 未覆盖（对它们设阈值会收到数据边界说明，"
                "需要这些指标时用 fund_history 自行计算）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "可选；先按主题关键词圈定池子再筛选，如「白酒」"},
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
            description=(
                "按 6 位基金代码取单只基金的完整字段（经理/主题/回撤/波动率/规模/费率等）。"
                "code 必须是 search_funds / screen_funds 结果里出现过的 6 位数字代码，禁止猜测。"
            ),
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
            description=(
                "按代码与周期（如 1M/3M/6M/1Y）取净值走势记录，用于看波动与回撤。"
                "code 必须是工具结果里出现过的 6 位数字代码，禁止猜测。"
            ),
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
    # 每个工具都包一层去重：同一轮研究内参数完全相同的重复调用返回缓存结果。
    wrapped = [
        dataclasses.replace(tool, execute_fn=_dedup_wrapper(tool.name, tool.execute_fn, state))
        for tool in tools
    ]
    return wrapped, state
