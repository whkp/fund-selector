"""基于 vendored tau agent 的基金研究 agent 服务。

设计（对应 app/llm.py 的旧单轮链路）：
- 模型端点不变（OpenAI-compatible，dashscope compatible-mode 实测支持
  function calling），但从「预投喂候选 + 单轮 JSON」升级为 tau 的
  AgentHarness 多轮工具循环：模型自己决定调 search_funds / screen_funds /
  get_fund_detail / fund_history / search_knowledge 拿真实数据，再下结论。
- 输出契约不变：最终一条 assistant 消息必须只含符合 RESEARCH_OUTPUT_SHAPE
  的 JSON，解析成 ModelResearchOutput —— 前端与研究记录结构零改动。
- ranking 校验基准从「预筛候选池」换成「工具结果里真实出现过的基金代码」
  （agent_tools 的 state["seen_codes"]），agent 检索到什么才能引用什么。
- 失败一律抛 LLMError 家族异常，由 main.py 既有分支处理/降级到旧链路。

默认关闭（llm.agent_mode = false），本地 config 或环境变量
FUND_COMPASS_LLM_AGENT_MODE 打开；关闭时行为与旧链路完全一致，测试桩不受影响。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from tau_agent import AgentHarness, AgentHarnessConfig, MessageEndEvent, ToolExecutionEndEvent
from tau_agent.messages import AssistantMessage, TextContent, UserMessage
from tau_ai import OpenAICompatibleConfig, OpenAICompatibleProvider

from .config import config_value
from .llm import RESEARCH_OUTPUT_SHAPE, LLMError, LLMResponseError, LLMService, ModelResearchOutput


class AgentResearchService:
    """把 vendored tau harness 接上基金领域的工具与输出契约。"""

    MAX_TURNS = 8

    def __init__(self, llm_service: LLMService, repository: Any, knowledge_base: Any) -> None:
        self.llm = llm_service
        self.repository = repository
        self.knowledge_base = knowledge_base
        self.enabled = (
            str(config_value("llm", "agent_mode", False, env_name="FUND_COMPASS_LLM_AGENT_MODE")).lower()
            == "true"
            and llm_service.status()["status"] == "READY"
            and llm_service.provider == "openai-compatible"
        )
        self._provider: OpenAICompatibleProvider | None = None
        self._tools: list[Any] | None = None
        self._tool_state: dict[str, Any] | None = None

    @property
    def provider(self) -> str:
        return self.llm.provider

    @property
    def model(self) -> str:
        return self.llm.model

    def _build(self) -> tuple[OpenAICompatibleProvider, list[Any], dict[str, Any]]:
        if self._provider is None:
            config = OpenAICompatibleConfig(
                api_key=self.llm.api_key,
                base_url=self.llm.base_url or "http://127.0.0.1:11434/v1",
                timeout_seconds=self.llm.timeout,
            )
            self._provider = OpenAICompatibleProvider(config)
            # 延迟 import 避免与 llm.py 的模块级依赖成环：agent_tools 只依赖 tau_*。
            from .agent_tools import build_fund_tools

            self._tools, self._tool_state = build_fund_tools(
                repository=self.repository,
                knowledge_base=self.knowledge_base,
                compact_fund=LLMService._compact_fund,
            )
        return self._provider, self._tools or [], self._tool_state or {}

    def _system_prompt(self) -> str:
        return (
            "你是基金研究助手，服务对象是正在挑选基金的个人投资者。\n"
            "\n"
            "## 工作方式（Agent）\n"
            "- 你有真实数据工具：search_funds（按主题/关键词搜索）、screen_funds（按收益/"
            "回撤/规模/费率/风险条件筛选排序）、get_fund_detail（单只完整字段）、"
            "fund_history（净值走势）、search_knowledge（研究知识库）。\n"
            "- 必须先用工具取得数据再下结论；结论里的每一个数字都必须来自工具返回，"
            "禁止编造或凭记忆补全。\n"
            "- 多角度验证：主题契合用 search_funds，横向对比用 screen_funds，"
            "引用回撤/规模等细节前用 get_fund_detail 确认。"
            "字段在工具结果里是 null 时，如实说明「未获取」。\n"
            "- 一般 2~4 次工具调用足够，不要为了刷次数重复调用同样的参数。\n"
            "\n"
            "## 分析方法\n"
            "1. 从问题提炼用户真实关注点：主题、风险偏好、期限、费率敏感度、规模偏好。\n"
            "2. 收益必须和风险一起看：高收益配大回撤的要指出；规模过小（低于 2 亿）有"
            "清盘风险，过大（超过百亿）调仓不灵活。\n"
            "3. 每个结论引用具体数字作证据，例如「A 基金 oneYear 32.4%，drawdown -18.2%，"
            "在候选中收益第二高但回撤最深」。\n"
            "4. score 按「与用户问题的契合度」打分，不是按收益高低排座次。\n"
            "5. 检索后确认没有与主题相关的基金时，在 ambiguities 里明确说明，"
            "ranking 可以为空数组。\n"
            "\n"
            "## 最终输出（严格遵守）\n"
            "研究完成后，最后一条回复必须**只**输出一个 JSON 对象，不要 Markdown、"
            "不要解释文字。字段名和层级必须与下面结构完全一致：\n"
            f"{RESEARCH_OUTPUT_SHAPE}\n"
            "ranking 中的 fundCode 必须是工具结果里出现过的基金代码，最多返回用户要求的数量。"
        )

    @staticmethod
    def _seed_history(history: list[dict[str, str]] | None) -> list[Any]:
        messages: list[Any] = []
        for turn in history or []:
            role = turn.get("role")
            content = str(turn.get("content", "")).strip()
            if role == "user" and content:
                messages.append(UserMessage(content=content[:600]))
            elif role == "assistant" and content:
                messages.append(AssistantMessage(model="history", content=[TextContent(text=content[:600])]))
        return messages

    @staticmethod
    def _extract_json(text: str) -> ModelResearchOutput:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        # 模型（实测 qwen3.8-flash）爱在长 JSON 里留尾逗号；旧链路有
        # response_format=json_object 兜底，agent 循环的工具轮没法带这个参数，
        # 最终轮只能靠提示词约束 —— 这里补一层确定性清洗。
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        return ModelResearchOutput.model_validate_json(cleaned)

    async def run_research(
        self,
        query: str,
        history: list[dict[str, str]] | None = None,
        limit: int = 10,
    ) -> tuple[ModelResearchOutput, list[dict[str, str]]]:
        """跑完整 agent 循环，返回 (结构化研究结果, 工具调用轨迹)。"""
        if not self.enabled:
            raise LLMError("Agent 模式未启用")
        provider, tools, state = self._build()
        state["seen_codes"] = set()
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=provider,
                model=self.llm.model,
                system=self._system_prompt(),
                tools=tools,
                max_turns=self.MAX_TURNS,
            ),
            messages=self._seed_history(history),
        )
        trace: list[dict[str, str]] = []
        final_text = ""

        async def _consume(events: AsyncIterator[Any]) -> None:
            nonlocal final_text
            async for event in events:
                if isinstance(event, ToolExecutionEndEvent):
                    result_text = event.result.text if event.result is not None else ""
                    detail = f"工具 {event.tool_name} 执行{'成功' if not event.is_error else '失败'}"
                    if not event.is_error:
                        try:
                            payload = json.loads(result_text)
                            if isinstance(payload, dict) and "matches" in payload:
                                detail += f"，命中 {len(payload['matches'])} 只"
                        except (ValueError, TypeError):
                            pass
                    trace.append({
                        "event": "tool_completed",
                        "title": f"调用工具 {event.tool_name}",
                        "detail": detail,
                        "status": "COMPLETED" if not event.is_error else "FAILED",
                    })
                elif isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
                    if event.message.stop_reason not in {"error", "aborted"}:
                        final_text = "".join(
                            block.text for block in event.message.content if isinstance(block, TextContent)
                        )

        await _consume(harness.prompt(query))

        if not final_text.strip():
            raise LLMResponseError("Agent 循环结束但没有产出最终回答")

        def _try_parse(text: str) -> ModelResearchOutput | None:
            try:
                return self._extract_json(text)
            except Exception:  # noqa: BLE001 - 解析失败走重试/降级，不携带细节
                return None

        output = _try_parse(final_text)
        if output is None:
            # 最终输出不是合法 JSON（实测长输出时模型会出尾逗号/漏冒号等格式错）。
            # 让模型自己修一次比正则打地鼠可靠：把坏文本原样退回，要求只回 JSON。
            repair_prompt = (
                "你上一条回复不是合法 JSON，无法被程序解析。请重新输出：只输出一个 JSON 对象，"
                "字段与之前要求的结构完全一致（intent/themes/ambiguities/summary/ranking/"
                "followUpQuestions），不要 Markdown 代码块，不要任何解释文字。"
            )
            harness.append_message(UserMessage(content=repair_prompt))
            final_text = ""
            await _consume(harness.continue_())
            output = _try_parse(final_text)
        if output is None:
            raise LLMResponseError(
                f"Agent 最终输出不是符合约定的 JSON（含修复重试）：{final_text[:120]}"
            )

        valid_codes = state["seen_codes"] or {fund.code for fund in self.repository.list_funds()}
        if any(item.fundCode not in valid_codes for item in output.ranking):
            raise LLMResponseError("Agent 返回了工具结果之外的基金代码")
        seen: set[str] = set()
        unique = []
        for item in output.ranking:
            if item.fundCode not in seen:
                unique.append(item)
                seen.add(item.fundCode)
        output.ranking = unique[:limit]
        return output, trace
