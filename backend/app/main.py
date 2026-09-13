from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth as auth_module
from . import watchlist as watchlist_module
from .auth import ConversationNotFound, EmailAlreadyRegistered, current_user
from .config import config_value
from .db.base import UserRecord
from .db.session import database_health
from .knowledge import build_knowledge_base
from .llm import LLMConfigurationError, LLMError, LLMNotConfigured, LLMResponseError, LLMService
from .models import Fund
from .providers import AKShareProvider, DataRepository
from .security import EmailFormatError, PasswordPolicyError


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


async def _warm_up() -> None:
    """Warm the fund universe so the first request after a cold start is fast."""
    try:
        await repository.ensure_funds()
    except Exception:
        # Warm-up failure must not block startup; the first request retries.
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await auth_module.ensure_auth_schema()
    except Exception as exc:  # pragma: no cover - 取决于部署环境
        # 建表失败必须可见，但不能挡住整个服务：基金数据接口仍可读。
        print(f"[startup] 认证表初始化失败：{exc}", file=sys.stderr)
    # 邀请码只在这里出现一次，方便把码抄给被邀请的人；
    # `/api/auth/policy` 永远不回显它，否则等于公开挂在墙上。
    try:
        print(f"[startup] {auth_module.describe_invite_policy()}", file=sys.stderr)
    except Exception as exc:  # pragma: no cover - 取决于部署环境
        print(f"[startup] 邀请码策略读取失败：{exc}", file=sys.stderr)
    warmup = asyncio.create_task(_warm_up())
    try:
        yield
    finally:
        warmup.cancel()


app = FastAPI(title="Fund Compass API", version="0.1.0", lifespan=lifespan)
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
    # 留空则自动新建一个对话，供「复盘日志」串起多轮研究。
    conversationId: str = Field(default="", max_length=64)


class WatchRequest(BaseModel):
    fundCode: str
    note: str = ""
    reasonTags: list[str] = Field(default_factory=list)


RISK_RANK = {"低风险": 1, "中低风险": 2, "中风险": 3, "中高风险": 4, "高风险": 5}

# AKShare 的公开排行接口不返回风险等级、申购状态和成立年限，这些字段会落成"未获取"。
# 缺失不等于不合格：字段未知时放行，并记录到 unverified 里如实告知用户，
# 而不是静默地把全部候选清空 —— 那会让「暂未得到研究候选」变成必然结果。
UNKNOWN_VALUES = {"", "未获取", "未知", "未标注", "待核", "none", "null", "n/a", "-"}

TYPE_SEPARATORS = r"[-—－–/／·|]"


def is_known(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in UNKNOWN_VALUES


def type_matches(fund_type: Any, wanted: list[str]) -> bool:
    """按类型根段匹配，兼容 AKShare 的复合类型名。

    数据侧给的是 "混合型-偏股" / "指数型-海外股票" / "QDII-混合偏股"，
    界面给用户选的是 "混合型" / "指数型" 这类大类。精确相等会让每一次
    类型筛选都返回空集，所以这里比较切分后的根段。
    """
    targets = [item.strip() for item in wanted if item and item.strip()]
    if not targets:
        return True
    source = str(fund_type or "").strip()
    if not source:
        return False
    segments = [segment.strip() for segment in re.split(TYPE_SEPARATORS, source) if segment.strip()]
    for target in targets:
        if source == target or source.startswith(target) or target in segments:
            return True
    return False


def eligibility(fund: Fund, filters: Filters) -> tuple[bool, list[str]]:
    """硬约束判定。返回 (是否通过, 因数据缺失而未能真正参与判定的字段)。"""
    unverified: list[str] = []
    if not type_matches(fund.type, filters.fundTypes):
        return False, unverified
    if filters.riskLevelMax:
        if not is_known(fund.risk):
            unverified.append("风险等级")
        elif RISK_RANK.get(fund.risk, 99) > RISK_RANK.get(filters.riskLevelMax, 99):
            return False, unverified
    if filters.maxFee:
        if fund.fee is None:
            unverified.append("管理费")
        elif fund.fee > filters.maxFee:
            return False, unverified
    if filters.requireOpen:
        if not is_known(fund.intake):
            unverified.append("申购状态")
        elif fund.intake != "开放申购":
            return False, unverified
    if filters.minimumInceptionYears:
        if fund.inception is None:
            unverified.append("成立年限")
        elif fund.inception < filters.minimumInceptionYears:
            return False, unverified
    return True, unverified


def unverified_note(funds: list[Fund], filters: Filters) -> str:
    """哪些硬约束因数据缺失而没能生效 —— 写进研究 trace，避免用户误判筛选已生效。"""
    if not funds:
        return ""
    tally: dict[str, int] = {}
    for fund in funds:
        _, missing = eligibility(fund, filters)
        for field in missing:
            tally[field] = tally.get(field, 0) + 1
    if not tally:
        return ""
    ordered = sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))
    detail = "、".join(f"{field} {count}/{len(funds)} 只未获取" for field, count in ordered)
    return f"AKShare 公开排行未提供 {detail}，这些字段未参与筛除。"


def empty_pool_reason(filters: Filters, universe: list[Fund]) -> str:
    """候选为空时，指出是哪一条约束把集合清空了，而不是只说一句"没有候选"。"""
    reasons: list[str] = []
    if filters.fundTypes:
        if not any(type_matches(fund.type, filters.fundTypes) for fund in universe):
            available = sorted({re.split(TYPE_SEPARATORS, str(fund.type or ""))[0].strip() for fund in universe if fund.type})
            reasons.append(
                f"类型「{'、'.join(filters.fundTypes)}」在当前数据源中没有匹配项（现有类型：{'、'.join(available) or '无'}）"
            )
    if filters.maxFee:
        if not any(fund.fee is None or fund.fee <= filters.maxFee for fund in universe):
            reasons.append(f"管理费上限 {filters.maxFee}% 没有基金满足")
    if filters.riskLevelMax:
        cap = RISK_RANK.get(filters.riskLevelMax, 99)
        if not any((not is_known(fund.risk)) or RISK_RANK.get(fund.risk, 99) <= cap for fund in universe):
            reasons.append(f"最高风险等级「{filters.riskLevelMax}」下没有基金")
    return ("；".join(reasons) + "。") if reasons else ""


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
        passed, _ = eligibility(fund, request.filters)
        if not passed:
            continue
        clone = Fund(**{**fund.__dict__, "score": query_score(fund, request.query)})
        result.append(clone)
    return sorted(result, key=lambda item: (-item.score, item.code))[:request.limit]


def research_pool(request: ScreenRequest) -> list[Fund]:
    """Apply only explicit eligibility constraints; ranking belongs to the model."""
    result = []
    for fund in repository.list_funds():
        passed, _ = eligibility(fund, request.filters)
        if not passed:
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


# ---------------------------------------------------------------------------
# 认证与会话
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    displayName: str = Field(default="", max_length=80)
    # 默认空串：处于开放注册的部署可以不带这个字段。
    inviteCode: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class ConversationCreateRequest(BaseModel):
    title: str = Field(default="", max_length=120)


def _session_payload(user: UserRecord) -> dict[str, Any]:
    token, expires_at = auth_module.issue_session_token(user)
    return {
        "token": token,
        "expiresAt": expires_at,
        "tokenType": "Bearer",
        "user": auth_module.user_public(user),
    }


@app.post("/api/auth/register", status_code=201)
async def register(request: RegisterRequest) -> dict[str, Any]:
    """注册并直接返回登录态。口令强度由 security 层统一校验。"""
    # 邀请码放在最前面。没通过准入的请求不会进建号逻辑：既省下 PBKDF2 的
    # 24 万次迭代，也避免用 409 告诉对方「这个邮箱已经注册过」。
    if not auth_module.verify_invite_code(request.inviteCode):
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_INVITE_CODE", "message": "邀请码不正确"})
    try:
        user = await auth_module.create_user(request.email, request.password, request.displayName)
    except EmailAlreadyRegistered as exc:
        raise HTTPException(status_code=409, detail={
            "code": "EMAIL_ALREADY_REGISTERED", "message": "该邮箱已注册，请直接登录"}) from exc
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "WEAK_PASSWORD", "message": str(exc)}) from exc
    except EmailFormatError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_EMAIL", "message": str(exc)}) from exc
    return _session_payload(user)


@app.post("/api/auth/login")
async def login(request: LoginRequest) -> dict[str, Any]:
    user = await auth_module.authenticate(request.email, request.password)
    if user is None:
        # 不区分「账号不存在」与「密码错误」，避免账号枚举。
        raise HTTPException(status_code=401, detail={
            "code": "INVALID_CREDENTIALS", "message": "邮箱或密码不正确"})
    return _session_payload(user)


@app.get("/api/auth/policy")
async def auth_policy() -> dict[str, Any]:
    """登录/注册页需要的公开信息。

    只回答「要不要邀请码」，不带码本身、也不带码的长度——这个端点不需要
    登录就能访问，泄露任何一位都会缩短爆破空间。
    """
    return auth_module.invite_policy()


@app.get("/api/auth/me")
async def me(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return {"user": auth_module.user_public(user)}


@app.post("/api/auth/logout", status_code=204)
async def logout(user: UserRecord = Depends(current_user)) -> None:
    """JWT 是无状态的：登出即由客户端丢弃令牌。保留端点便于前端统一调用。"""
    return None


@app.get("/api/conversations")
async def conversations(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return {"items": await auth_module.list_conversations(user.id)}


@app.post("/api/conversations", status_code=201)
async def create_conversation(
    request: ConversationCreateRequest, user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
    return await auth_module.create_conversation(user.id, request.title)


@app.get("/api/conversations/{conversation_id}")
async def conversation_detail(
    conversation_id: str, user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
    try:
        conversation = await auth_module.get_conversation(user.id, conversation_id)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "code": "CONVERSATION_NOT_FOUND", "message": "对话不存在"}) from exc
    return {
        "conversation": conversation,
        "messages": await auth_module.list_messages(user.id, conversation_id),
    }


@app.delete("/api/conversations/{conversation_id}", status_code=204)
async def remove_conversation(
    conversation_id: str, user: UserRecord = Depends(current_user)
) -> None:
    try:
        await auth_module.delete_conversation(user.id, conversation_id)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "code": "CONVERSATION_NOT_FOUND", "message": "对话不存在"}) from exc


@app.get("/api/ai/status")
async def ai_status(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return llm_service.status()


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


@app.get("/api/knowledge/status")
async def knowledge_status(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return knowledge_base.status()


@app.post("/api/knowledge/search")
async def knowledge_search(
    request: KnowledgeSearchRequest, user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
    items = await knowledge_base.search(request.query, request.limit)
    return {"items": [item.__dict__ for item in items], "status": knowledge_base.status()}


@app.post("/api/ai/configure")
async def configure_ai() -> Any:
    raise HTTPException(403, detail={"code": "SERVER_SIDE_CONFIGURATION_ONLY", "message": "公开模式只允许通过服务端环境变量配置模型"})


@app.post("/api/ai/reset")
async def reset_ai() -> Any:
    raise HTTPException(403, detail={"code": "SERVER_SIDE_CONFIGURATION_ONLY", "message": "公开模式只允许通过服务端环境变量切换模型"})


@app.get("/api/funds")
async def funds(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    return envelope([fund.as_dict() for fund in repository.list_funds()])


@app.get("/api/funds/{code}")
async def fund_detail(code: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    fund = repository.get_fund(code)
    if not fund:
        raise HTTPException(404, detail={"code": "FUND_NOT_FOUND", "message": "基金不存在"})
    return {"fund": fund.as_dict(), **{key: value for key, value in envelope([]).items() if key != "items"}}


@app.get("/api/funds/{code}/data-quality")
async def fund_quality(code: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
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
async def fund_history(
    code: str, period: str = "1年", user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
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
async def screen(
    request: ScreenRequest, user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    return envelope([item.as_dict() for item in filtered(request)])


@app.post("/api/funds/compare")
async def compare(
    payload: dict[str, Any], user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
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
async def create_run(
    request: ScreenRequest, user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
    require_production_data()
    require_public_research()
    await repository.ensure_funds()
    # 每条研究记录都归属一个对话，让「复盘日志」能串起多轮追问。
    # 先判定候选池再落库：条件不成立时不该在「复盘日志」里留下一问无答的空对话。
    items = research_pool(request)
    if not items:
        raise HTTPException(status_code=400, detail={
            "code": "NO_ELIGIBLE_CANDIDATES",
            "message": "当前条件下没有符合条件的基金候选。" + empty_pool_reason(request.filters, repository.list_funds()),
        })
    # 追问时先确认对话归属，避免为一个不存在的对话白跑一次模型。
    requested_conversation = request.conversationId.strip()
    if requested_conversation:
        try:
            await auth_module.get_conversation(user.id, requested_conversation)
        except ConversationNotFound as exc:
            raise HTTPException(status_code=404, detail={
                "code": "CONVERSATION_NOT_FOUND", "message": "对话不存在"}) from exc
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
    # 模型确实产出了结论，此时才建对话并写入这一轮问答。
    conversation_id = requested_conversation or (await auth_module.create_conversation(user.id, request.query))["id"]
    await auth_module.append_message(
        user.id, conversation_id, "user", request.query,
        payload={"filters": request.filters.model_dump(), "limit": request.limit},
    )
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
                     {"event": "tool_completed", "title": "检索真实候选", "detail": f"从 {MODE} AKShare 数据快照取得 {len(items)} 只符合硬约束的候选。{unverified_note(items, request.filters)}", "status": "COMPLETED"},
                     {"event": "llm_completed", "title": "大模型生成研究结论", "detail": "模型输出已按候选代码和结构化 Schema 校验。", "status": "COMPLETED"}],
           "disclaimer": "内容仅供基金研究参考，不构成投资建议。模型不生成交易指令。", "nextQuestions": model_output.followUpQuestions}
    run["conversationId"] = conversation_id
    repository.runs[run_id] = run
    await auth_module.append_message(
        user.id, conversation_id, "assistant", run["summary"], run_id=run_id, payload=run,
    )
    return run


async def _load_run(run_id: str, user: UserRecord) -> dict[str, Any]:
    """先查内存快路径，再回落到对话记录，进程重启后仍能复盘。"""
    run = repository.runs.get(run_id)
    if run is not None:
        return run
    message = await auth_module.find_message_by_run(user.id, run_id)
    payload = (message or {}).get("payload")
    if isinstance(payload, dict):
        return payload
    raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "研究记录不存在"})


@app.get("/api/recommendations/runs/{run_id}")
async def get_run(run_id: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return await _load_run(run_id, user)


@app.get("/api/recommendations/runs/{run_id}/trace")
async def get_trace(run_id: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    run = await _load_run(run_id, user)
    return {"runId": run_id, "trace": run["trace"], "policyVersion": run["policyVersion"], "modelVersion": run["modelVersion"], "snapshotId": run["snapshotId"]}


@app.get("/api/recommendations/runs/{run_id}/events")
async def get_events(run_id: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    run = await _load_run(run_id, user)
    return {"runId": run_id, "events": run["trace"]}


@app.get("/api/market/etf-quotes")
async def market_quotes(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    status = provider.status.as_dict()
    items = await provider.etf_quotes()
    status = provider.status.as_dict()
    status.update({"sourceName": "AKShare ETF public reference", "sourceType": "AKSHARE_PUBLIC"})
    return {"items": items, "status": status, "disclaimer": "场内交易价格仅作 ETF/LOF 行情参考，不等于基金正式净值，也不参与当前推荐排序。"}


@app.post("/api/market/etf-quotes/refresh")
async def refresh_market_quotes(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return await market_quotes(user)


@app.get("/api/data-sources/status")
async def data_sources(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return {"items": [provider.status.as_dict()], "mode": MODE, "nav": provider.status.as_dict()}


@app.post("/api/data/funds/refresh")
async def refresh_funds(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    require_public_write()
    updated = await repository.refresh_funds()
    return {"updated": updated, "status": provider.status.as_dict(), "count": len(repository.funds),
            "message": "基金目录已刷新" if updated else "基金目录刷新未产生新记录，请检查数据源状态"}


@app.get("/api/watchlist/items")
async def list_watchlist(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    await repository.ensure_funds()
    return {"items": await watchlist_module.list_items(user.id, repository.get_fund)}


@app.post("/api/watchlist/items", status_code=201)
async def add_watch(
    request: WatchRequest, user: UserRecord = Depends(current_user)
) -> dict[str, Any]:
    await repository.ensure_funds()
    fund = repository.get_fund(request.fundCode)
    if not fund:
        raise HTTPException(404, detail={"code": "FUND_NOT_FOUND", "message": "基金不存在"})
    return await watchlist_module.add_item(user.id, fund, request.note, request.reasonTags)


@app.delete("/api/watchlist/items/{code}", status_code=204)
async def remove_watch(code: str, user: UserRecord = Depends(current_user)) -> None:
    if not await watchlist_module.remove_item(user.id, code):
        raise HTTPException(404, detail={"code": "WATCHLIST_ITEM_NOT_FOUND", "message": "观察列表中没有该基金"})


@app.get("/api/profile/risk")
async def get_profile(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
    return {"riskLevel": "中高风险", "investmentHorizonMonths": 36, "liquidityNeed": "低", "goalType": "长期积累",
            "confirmedAt": datetime.now(timezone.utc).isoformat(), "questionnaireVersion": "risk-questionnaire-v1"}


# ---------------------------------------------------------------------------
# Same-origin static frontend
# ---------------------------------------------------------------------------
# When a Vite build sits next to the backend, serve it from this same app so a
# single process answers both the API and the UI. That keeps the browser on one
# origin (no CORS negotiation) and lets a single tunnel expose a single port.
# Registered last so every /api and operational route declared above wins the
# match; StaticFiles only sees what nothing else claimed. Deployments that ship
# without a build (Render, containers) simply skip the mount and behave as before.
_frontend_dir = Path(__file__).resolve().parents[2] / "dist"
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
