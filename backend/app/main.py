from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import config_value
from .db.session import database_health
from .knowledge import build_knowledge_base
from .llm import LLMConfigurationError, LLMError, LLMNotConfigured, LLMResponseError, LLMService
from .models import Fund
from .providers import AKShareProvider, DataRepository


MODE = str(config_value("app", "mode", "REFERENCE", env_name="FUND_COMPASS_MODE")).upper()
if MODE not in {"LOCAL", "REFERENCE", "PRODUCTION"}:
    MODE = "REFERENCE"
PRODUCTION_DATA_READY = str(config_value(
    "app", "production_data_ready", False, env_name="FUND_COMPASS_PRODUCTION_DATA_READY"
)).lower() == "true"
PUBLIC_WRITE_ENABLED = str(config_value(
    "app", "public_write_enabled", False, env_name="FUND_COMPASS_PUBLIC_WRITE_ENABLED"
)).lower() == "true"
PUBLIC_RESEARCH_ENABLED = str(config_value(
    "app", "public_research_enabled", MODE == "REFERENCE", env_name="FUND_COMPASS_PUBLIC_RESEARCH_ENABLED"
)).lower() == "true"
provider = AKShareProvider()
repository = DataRepository(provider)
llm_service = LLMService()
knowledge_base = build_knowledge_base()
app = FastAPI(title="Fund Compass API", version="0.1.0")
cors_origins = str(config_value(
    "app", "cors_origins", "http://127.0.0.1:4173,http://localhost:4173",
    env_name="FUND_COMPASS_CORS_ORIGINS"
))
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in cors_origins.split(",") if item.strip()],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", f"req_{uuid.uuid4().hex[:12]}")
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "服务内部错误", "requestId": request_id, "retryable": False}},
        )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def require_public_write() -> None:
    if MODE != "LOCAL" and not PUBLIC_WRITE_ENABLED:
        raise HTTPException(status_code=403, detail={
            "code": "PUBLIC_WRITE_DISABLED", "message": "当前运行模式不开放匿名写操作"
        })


def require_public_research() -> None:
    if MODE != "LOCAL" and not PUBLIC_RESEARCH_ENABLED:
        raise HTTPException(status_code=403, detail={
            "code": "PUBLIC_RESEARCH_DISABLED", "message": "当前运行模式未开放匿名模型研究"
        })


def require_production_data() -> None:
    if MODE == "PRODUCTION" and not PRODUCTION_DATA_READY:
        raise HTTPException(status_code=503, detail={
            "code": "PRODUCTION_DATA_NOT_READY", "message": "生产数据源尚未完成配置", "retryable": False
        })


class Filters(BaseModel):
    fundTypes: list[str] = Field(default_factory=list)
    riskLevelMax: str = ""
    maxFee: float = 0
    requireOpen: bool = False
    minimumInceptionYears: float = 0


class LLMRequestConfig(BaseModel):
    provider: str = Field(default="openai-compatible", max_length=40)
    baseUrl: str = Field(default="", max_length=500)
    apiKey: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=160)
    timeoutSeconds: float = Field(default=45, ge=5, le=120)


class ScreenRequest(BaseModel):
    query: str = ""
    filters: Filters = Field(default_factory=Filters)
    limit: int = Field(default=10, ge=1, le=50)
    llm: LLMRequestConfig | None = None


class WatchRequest(BaseModel):
    fundCode: str
    note: str = ""
    reasonTags: list[str] = Field(default_factory=list)


RISK_RANK = {"低风险": 1, "中低风险": 2, "中风险": 3, "中高风险": 4, "高风险": 5}


def envelope(items: Any) -> dict[str, Any]:
    source = provider.status.as_dict()
    quality = "REFERENCE" if MODE != "PRODUCTION" else ("ACTIVE" if PRODUCTION_DATA_READY else "UNAVAILABLE")
    return {"items": items, "asOf": datetime.now(timezone.utc).date().isoformat(),
            "snapshotId": f"{MODE.lower()}-snapshot", "qualityStatus": quality,
            "mode": MODE, "dataMode": MODE, "sourceType": source["sourceType"],
            "trustLevel": source["trustLevel"]}


def query_score(fund: Fund, query: str) -> int:
    text = query.lower()
    haystack = " ".join([fund.code, fund.name, fund.short_name, fund.company, fund.theme, *fund.tags]).lower()
    matches = sum(1 for token in re.split(r"[，、,\s;；。]+", text) if len(token) >= 2 and token in haystack)
    return min(100, fund.score + matches * 8)


def filtered(request: ScreenRequest) -> list[Fund]:
    result = []
    for fund in repository.list_funds():
        filters = request.filters
        if filters.fundTypes and fund.type not in filters.fundTypes:
            continue
        if filters.riskLevelMax and (fund.risk not in RISK_RANK or RISK_RANK[fund.risk] > RISK_RANK.get(filters.riskLevelMax, 99)):
            continue
        if filters.maxFee and (fund.fee is None or fund.fee > filters.maxFee):
            continue
        if filters.requireOpen and fund.intake != "开放申购":
            continue
        if filters.minimumInceptionYears and (fund.inception is None or fund.inception < filters.minimumInceptionYears):
            continue
        clone = Fund(**{**fund.__dict__, "score": query_score(fund, request.query)})
        result.append(clone)
    return sorted(result, key=lambda item: (-item.score, item.code))[:request.limit]


def research_pool(request: ScreenRequest) -> list[Fund]:
    """Apply only explicit eligibility constraints; ranking belongs to the model."""
    result = []
    for fund in repository.list_funds():
        filters = request.filters
        if filters.fundTypes and fund.type not in filters.fundTypes:
            continue
        if filters.riskLevelMax and (fund.risk not in RISK_RANK or RISK_RANK[fund.risk] > RISK_RANK.get(filters.riskLevelMax, 99)):
            continue
        if filters.maxFee and (fund.fee is None or fund.fee > filters.maxFee):
            continue
        if filters.requireOpen and fund.intake != "开放申购":
            continue
        if filters.minimumInceptionYears and (fund.inception is None or fund.inception < filters.minimumInceptionYears):
            continue
        result.append(fund)
    # Keep the prompt bounded while preserving the provider's current reference order.
    return result[:60]


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "mode": MODE.lower(), "provider": "python-fastapi",
            "dataSource": provider.status.as_dict(), "database": await database_health()}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    if MODE == "PRODUCTION" and not PRODUCTION_DATA_READY:
        raise HTTPException(status_code=503, detail={"code": "PRODUCTION_DATA_NOT_READY", "message": "生产数据源尚未完成配置"})
    db = await database_health()
    if db.get("status") != "ACTIVE":
        raise HTTPException(status_code=503, detail={"code": "DATABASE_UNAVAILABLE", "message": "数据库暂不可用", "retryable": True})
    return {"status": "ready", "mode": MODE.lower(), "dataSource": provider.status.as_dict(), "database": db}


@app.get("/api/ai/status")
async def ai_status() -> dict[str, Any]:
    return llm_service.status()


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


@app.get("/api/knowledge/status")
async def knowledge_status() -> dict[str, Any]:
    return knowledge_base.status()


@app.post("/api/knowledge/search")
async def knowledge_search(request: KnowledgeSearchRequest) -> dict[str, Any]:
    items = await knowledge_base.search(request.query, request.limit)
    return {"items": [item.__dict__ for item in items], "status": knowledge_base.status()}


@app.post("/api/ai/configure")
async def configure_ai() -> Any:
    raise HTTPException(403, detail={"code": "SERVER_SIDE_CONFIGURATION_ONLY", "message": "公开模式只允许通过服务端环境变量配置模型"})


@app.post("/api/ai/reset")
async def reset_ai() -> Any:
    raise HTTPException(403, detail={"code": "SERVER_SIDE_CONFIGURATION_ONLY", "message": "公开模式只允许通过服务端环境变量切换模型"})


@app.get("/api/funds")
async def funds() -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    return envelope([fund.as_dict() for fund in repository.list_funds()])


@app.get("/api/funds/{code}")
async def fund_detail(code: str) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    fund = repository.get_fund(code)
    if not fund:
        raise HTTPException(404, detail={"code": "FUND_NOT_FOUND", "message": "基金不存在"})
    return {"fund": fund.as_dict(), **{key: value for key, value in envelope([]).items() if key != "items"}}


@app.get("/api/funds/{code}/data-quality")
async def fund_quality(code: str) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    fund = repository.get_fund(code)
    if not fund:
        raise HTTPException(404, detail={"code": "FUND_NOT_FOUND", "message": "基金不存在"})
    reports = [{"fundCode": code, "fieldName": "unitNav", "freshnessStatus": fund.nav_freshness,
                "trustLevel": fund.nav_trust_level, "asOf": fund.nav_date, "sourceName": fund.source,
                "snapshotId": fund.snapshot}, {"fundCode": code, "fieldName": "metrics.1Y",
                "freshnessStatus": "MISSING", "trustLevel": "LOW",
                "asOf": fund.nav_date, "sourceName": fund.source, "snapshotId": fund.snapshot}]
    return envelope(reports)


@app.get("/api/funds/{code}/history")
async def fund_history(code: str, period: str = "1年") -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    fund = repository.get_fund(code)
    if not fund:
        raise HTTPException(404, detail={"code": "FUND_NOT_FOUND", "message": "基金不存在"})
    records = await repository.history(code, period)
    if not records:
        return {**envelope([]), "fundCode": code, "period": period,
                "metricStatus": "UNAVAILABLE", "metricVersion": "metric-v1",
                "sourceStatus": provider.status.as_dict()}
    from .metrics import calculate_metrics
    metrics = calculate_metrics([record["nav"] for record in records])
    return {**envelope(records), "fundCode": code, "period": period,
            "metrics": metrics, "metricStatus": metrics.get("status"),
            "metricVersion": metrics.get("metricVersion", "metric-v1"),
            "sourceStatus": provider.status.as_dict()}


@app.post("/api/funds/screen")
async def screen(request: ScreenRequest) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    return envelope([item.as_dict() for item in filtered(request)])


@app.post("/api/funds/compare")
async def compare(payload: dict[str, Any]) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    codes = payload.get("codes", [])
    if not 1 <= len(codes) <= 4:
        raise HTTPException(400, detail={"code": "INVALID_COMPARE_SET", "message": "对比必须包含 1 到 4 只基金"})
    items = []
    for code in codes:
        fund = repository.get_fund(code)
        if not fund:
            raise HTTPException(404, detail={"code": "FUND_NOT_FOUND", "message": "对比基金不存在"})
        items.append(fund.as_dict())
    return {**envelope(items), "window": payload.get("window", "1Y"), "metricVersion": "metric-v1"}


@app.post("/api/recommendations/runs")
async def create_run(request: ScreenRequest) -> dict[str, Any]:
    require_production_data()
    require_public_research()
    await repository.ensure_funds()
    items = research_pool(request)
    knowledge = await knowledge_base.search(request.query, limit=5)
    research_llm = llm_service
    try:
        if request.llm is not None:
            research_llm = llm_service.for_session_request(request.llm.model_dump(exclude_none=True))
        model_output = await research_llm.research(request.query, items, knowledge, request.limit)
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_LLM_CONFIGURATION", "message": str(exc)}) from exc
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail={"code": "LLM_NOT_CONFIGURED", "message": str(exc)}) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail={"code": "LLM_RESPONSE_INVALID", "message": str(exc)}) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail={"code": "LLM_UNAVAILABLE", "message": str(exc)}) from exc
    by_code = {item.code: item for item in items}
    ranked_funds: list[tuple[Fund, Any]] = []
    for assessment in model_output.ranking:
        original = by_code.get(assessment.fundCode)
        if not original:
            continue
        ranked_funds.append((Fund(**{**original.__dict__, "score": assessment.score,
                                     "reason": assessment.reason, "highlights": [assessment.fit],
                                     "caveat": "；".join(assessment.riskFlags) or original.caveat}), assessment))
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    run = {"runId": run_id, "status": "COMPLETED", "mode": f"{MODE}_LLM_RESEARCH",
           "originalQuery": request.query, "createdAt": datetime.now(timezone.utc).isoformat(),
           "completedAt": datetime.now(timezone.utc).isoformat(), "policyVersion": "research-policy-v1",
           "modelVersion": f"{research_llm.provider}:{research_llm.model}", "snapshotId": f"{MODE.lower()}-snapshot",
           "interpretation": {"intent": model_output.intent, "themes": model_output.themes,
                              "ambiguities": model_output.ambiguities, "provider": research_llm.provider, "status": "COMPLETED"},
           "summary": model_output.summary, "knowledgeRefs": [item.__dict__ for item in knowledge],
           "candidates": [{"fund": fund.as_dict(), "rank": index + 1, "decision": "LLM_REFERENCE_CANDIDATE",
                           "hardFilterPassed": True, "evidenceRefs": [f"profile:{fund.code}:{fund.snapshot}", f"nav:{fund.code}:{fund.nav_date}"],
                           "riskFlags": assessment.riskFlags, "analysis": assessment.fit,
                           "dataFreshness": {"nav": {"status": fund.nav_freshness, "asOf": fund.nav_date}, "metrics": {"status": "MISSING", "asOf": fund.nav_date}},
                           "sourceSummary": {"primarySourceType": fund.nav_source_type, "officialDisclosureChecked": False, "lowestTrustLevel": fund.nav_trust_level},
                           "recommendationScore": assessment.score} for index, (fund, assessment) in enumerate(ranked_funds)],
           "trace": [{"event": "plan_created", "title": "理解研究目标", "detail": model_output.intent, "status": "COMPLETED"},
                     {"event": "knowledge_retrieved", "title": "检索研究知识库", "detail": f"检索到 {len(knowledge)} 个可引用片段。", "status": "COMPLETED"},
                     {"event": "tool_completed", "title": "检索真实候选", "detail": f"从 {MODE} AKShare 数据快照取得 {len(items)} 只符合硬约束的候选。", "status": "COMPLETED"},
                     {"event": "llm_completed", "title": "大模型生成研究结论", "detail": "模型输出已按候选代码和结构化 Schema 校验。", "status": "COMPLETED"}],
           "disclaimer": "内容仅供基金研究参考，不构成投资建议。模型不生成交易指令。", "nextQuestions": model_output.followUpQuestions}
    repository.runs[run_id] = run
    return run


@app.get("/api/recommendations/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = repository.runs.get(run_id)
    if not run:
        raise HTTPException(404, detail={"code": "RUN_NOT_FOUND", "message": "研究记录不存在"})
    return run


@app.get("/api/recommendations/runs/{run_id}/trace")
async def get_trace(run_id: str) -> dict[str, Any]:
    run = await get_run(run_id)
    return {"runId": run_id, "trace": run["trace"], "policyVersion": run["policyVersion"], "modelVersion": run["modelVersion"], "snapshotId": run["snapshotId"]}


@app.get("/api/recommendations/runs/{run_id}/events")
async def get_events(run_id: str) -> dict[str, Any]:
    run = await get_run(run_id)
    return {"runId": run_id, "events": run["trace"]}


@app.get("/api/market/etf-quotes")
async def market_quotes() -> dict[str, Any]:
    status = provider.status.as_dict()
    items = await provider.etf_quotes()
    status = provider.status.as_dict()
    status.update({"sourceName": "AKShare ETF public reference", "sourceType": "AKSHARE_PUBLIC"})
    return {"items": items, "status": status, "disclaimer": "场内交易价格仅作 ETF/LOF 行情参考，不等于基金正式净值，也不参与当前推荐排序。"}


@app.post("/api/market/etf-quotes/refresh")
async def refresh_market_quotes() -> dict[str, Any]:
    return await market_quotes()


@app.get("/api/data-sources/status")
async def data_sources() -> dict[str, Any]:
    return {"items": [provider.status.as_dict()], "mode": MODE, "nav": provider.status.as_dict()}


@app.post("/api/data/funds/refresh")
async def refresh_funds() -> dict[str, Any]:
    require_public_write()
    updated = await repository.refresh_funds()
    return {"updated": updated, "status": provider.status.as_dict(), "count": len(repository.funds),
            "message": "基金目录已刷新" if updated else "基金目录刷新未产生新记录，请检查数据源状态"}


@app.get("/api/watchlist/items")
async def list_watchlist() -> dict[str, Any]:
    if MODE != "LOCAL" and not PUBLIC_WRITE_ENABLED:
        return {"items": []}
    return {"items": list(repository.watchlist.values())}


@app.post("/api/watchlist/items", status_code=201)
async def add_watch(request: WatchRequest) -> dict[str, Any]:
    require_public_write()
    fund = repository.get_fund(request.fundCode)
    if not fund:
        raise HTTPException(404, detail={"code": "FUND_NOT_FOUND", "message": "基金不存在"})
    return repository.add_watch(fund, request.note, request.reasonTags)


@app.delete("/api/watchlist/items/{code}", status_code=204)
async def remove_watch(code: str) -> None:
    require_public_write()
    if code not in repository.watchlist:
        raise HTTPException(404, detail={"code": "WATCHLIST_ITEM_NOT_FOUND", "message": "观察列表中没有该基金"})
    del repository.watchlist[code]


@app.get("/api/profile/risk")
async def get_profile() -> dict[str, Any]:
    return {"riskLevel": "中高风险", "investmentHorizonMonths": 36, "liquidityNeed": "低", "goalType": "长期积累",
            "confirmedAt": datetime.now(timezone.utc).isoformat(), "questionnaireVersion": "risk-questionnaire-v1"}
