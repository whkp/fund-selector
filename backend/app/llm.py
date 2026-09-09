from __future__ import annotations

import json
import os
import re
from ipaddress import ip_address
from urllib.parse import urlparse
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import config_value, llm_configuration_source
from .knowledge import KnowledgeChunk
from .models import Fund


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class LLMConfigurationError(LLMError):
    pass


class ModelAssessment(BaseModel):
    fundCode: str
    score: int = Field(ge=0, le=100)
    fit: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)
    riskFlags: list[str] = Field(default_factory=list, max_length=8)


class ModelResearchOutput(BaseModel):
    intent: str = Field(min_length=1, max_length=300)
    themes: list[str] = Field(default_factory=list, max_length=10)
    ambiguities: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(min_length=1, max_length=2000)
    ranking: list[ModelAssessment] = Field(default_factory=list, max_length=20)
    followUpQuestions: list[str] = Field(default_factory=list, max_length=8)


class LLMService:
    """OpenAI-compatible research adapter with strict, fact-grounded output."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        overrides = overrides or {}
        self.provider = str(overrides.get("provider", config_value(
            "llm", "provider", "openai-compatible", env_name="FUND_COMPASS_LLM_PROVIDER"
        ))).strip().lower()
        self.model = str(overrides.get("model", config_value(
            "llm", "model", "gpt-4o-mini", env_name="FUND_COMPASS_LLM_MODEL"
        ))).strip()
        self.base_url = str(overrides.get("baseUrl", overrides.get("base_url", config_value(
            "llm", "base_url", "", env_name="FUND_COMPASS_LLM_BASE_URL"
        )))).strip().rstrip("/")
        self.api_key = str(overrides.get("apiKey", overrides.get("api_key", config_value(
            "llm", "api_key", "", env_name="FUND_COMPASS_LLM_API_KEY"
        )))).strip()
        self.timeout = float(overrides.get("timeoutSeconds", overrides.get("timeout_seconds", config_value(
            "llm", "timeout_seconds", 45, env_name="FUND_COMPASS_LLM_TIMEOUT_SECONDS"
        ))))

    def for_session_request(self, overrides: dict[str, Any]) -> "LLMService":
        service = LLMService(overrides)
        service.validate_session_endpoint()
        return service

    def validate_session_endpoint(self) -> None:
        if self.provider not in {"openai-compatible", "ollama"}:
            raise LLMConfigurationError("暂不支持该模型 Provider")
        if not self.model or len(self.model) > 160:
            raise LLMConfigurationError("模型名称无效")
        if self.provider == "openai-compatible" and not self.api_key:
            raise LLMConfigurationError("会话模式必须提供 API Key")
        parsed = urlparse(self.base_url or "http://127.0.0.1:11434/v1")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise LLMConfigurationError("Base URL 必须是有效的 HTTP(S) 地址")
        is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if is_local:
            allow_local = str(config_value("app", "allow_local_llm", False, env_name="FUND_COMPASS_ALLOW_LOCAL_LLM")).lower() == "true"
            if not allow_local:
                raise LLMConfigurationError("公网会话不允许访问本机地址；本地测试请开启 FUND_COMPASS_ALLOW_LOCAL_LLM")
            return
        if parsed.scheme != "https":
            raise LLMConfigurationError("公网模型地址必须使用 HTTPS")
        try:
            if ip_address(parsed.hostname).is_private:
                raise LLMConfigurationError("不允许访问内网模型地址")
        except ValueError:
            pass
        allowed = {host.strip().lower() for host in str(os.getenv(
            "FUND_COMPASS_LLM_ALLOWED_HOSTS",
            "api.openai.com,api.deepseek.com,dashscope.aliyuncs.com,api.moonshot.cn,api.siliconflow.cn,openrouter.ai,api.z.ai"
        )).split(",") if host.strip()}
        if parsed.hostname.lower() not in allowed:
            raise LLMConfigurationError("该模型域名未在服务端白名单中")

    def status(self) -> dict[str, object]:
        configured = self.provider in {"openai-compatible", "ollama"} and bool(self.model)
        if self.provider == "openai-compatible":
            configured = configured and bool(self.base_url and self.api_key)
        return {
            "provider": self.provider,
            "model": self.model,
            "status": "READY" if configured else "NOT_CONFIGURED",
            "credentials": "server-side",
            "configurationSource": llm_configuration_source(),
            "message": "模型将根据真实候选数据生成结构化研究结果。" if configured else "请在服务端环境变量配置 LLM。",
        }

    async def research(self, query: str, funds: list[Fund], knowledge: list[KnowledgeChunk], limit: int) -> ModelResearchOutput:
        status = self.status()
        if status["status"] != "READY":
            raise LLMNotConfigured("LLM 未配置：请设置 FUND_COMPASS_LLM_PROVIDER、MODEL、BASE_URL 和 API_KEY")
        compact_funds = [self._compact_fund(fund) for fund in funds]
        kb_context = [{"id": item.chunk_id, "title": item.title, "content": item.content} for item in knowledge]
        system = (
            "你是基金研究助手。只允许使用用户提供的候选基金事实和知识库片段。"
            "你不能创建基金、补全缺失字段、编造数字、预测收益或给出买卖指令。"
            "请只返回 JSON，不要 Markdown。ranking 中的 fundCode 必须来自候选列表，最多返回用户要求的数量。"
            "对于未获取字段必须明确说未获取。summary、reason 和 riskFlags 要说明证据边界。"
        )
        user = json.dumps({"query": query, "limit": limit, "candidates": compact_funds, "knowledge": kb_context}, ensure_ascii=False)
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        base_url = self.base_url or "http://127.0.0.1:11434/v1"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError(f"LLM 请求失败: {str(exc)[:200]}") from exc
        try:
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip())
            result = ModelResearchOutput.model_validate_json(text)
        except (ValidationError, ValueError, TypeError) as exc:
            raise LLMResponseError("LLM 返回不是符合约定的结构化 JSON") from exc
        valid_codes = {fund.code for fund in funds}
        if any(item.fundCode not in valid_codes for item in result.ranking):
            raise LLMResponseError("LLM 返回了候选列表之外的基金代码")
        seen: set[str] = set()
        unique = []
        for item in result.ranking:
            if item.fundCode not in seen:
                unique.append(item)
                seen.add(item.fundCode)
        result.ranking = unique[:limit]
        return result

    @staticmethod
    def _compact_fund(fund: Fund) -> dict[str, Any]:
        return {
            "fundCode": fund.code, "name": fund.name, "type": fund.type, "risk": fund.risk,
            "nav": fund.nav, "navDate": fund.nav_date, "ytd": fund.ytd, "oneYear": fund.one_year,
            "fee": fund.fee, "intake": fund.intake, "source": fund.source,
            "sourceType": fund.nav_source_type, "trustLevel": fund.nav_trust_level,
            "caveat": fund.caveat,
        }
