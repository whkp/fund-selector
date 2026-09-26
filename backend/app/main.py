from __future__ import annotations

import asyncio
import logging
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth as auth_module
from . import ratelimit as ratelimit_module
from . import watchlist as watchlist_module
from .agent_service import AgentResearchService
from .auth import ConversationNotFound, EmailAlreadyRegistered, current_user
from .config import config_value
from .db import migrate as migrate_module
from .db.base import UserRecord
from .db.session import database_health
from .knowledge import build_knowledge_base
from .llm import LLMConfigurationError, LLMError, LLMNotConfigured, LLMResponseError, LLMService
from .models import Fund
from .providers import AKShareProvider, DataRepository
from .security import EmailFormatError, PasswordPolicyError

# 让 app 包的 INFO 日志真正可见：uvicorn 只为自己那两个 logger 配了 handler，
# 没有这段时 app.* 的 logger.info（warm-up 装载了几只、走没走 DB 快照、
# 补数回填多少）会因 root 只剩 lastResort(WARNING) 而静默丢失——线上出
# 问题时无从对账。第三方库保持安静：root 停在 WARNING，只放行 app 包。
logging.basicConfig(level=logging.WARNING)
logging.getLogger("app").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

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
# Agent 模式（tau harness 多轮工具循环）默认关闭；llm.agent_mode /
# FUND_COMPASS_LLM_AGENT_MODE 打开且 LLM 就绪时，研究走真实工具调用。
agent_research = AgentResearchService(llm_service, repository, knowledge_base)


async def _warm_up() -> None:
    """Warm the fund universe so the first request after a cold start is fast."""
    # 优先从数据库装载上次同步的目录快照：命中且未过 TTL 时连 AKShare 都不用
    # 打；过期时下面的 ensure_funds() 会做一次刷新，失败也有这份数据兜底。
    try:
        from .db.snapshot_repository import load_fund_universe

        snapshot = await load_fund_universe()
        if snapshot is not None:
            funds, fetched_at = snapshot
            await repository.adopt_universe(funds, fetched_at)
            logger.info("已从数据库装载基金目录 %s 只（快照时间 %s）", len(funds), fetched_at)
    except Exception as exc:  # noqa: BLE001 - 装载失败只是回落到实时抓取
        logger.warning("数据库目录快照装载失败：%s", exc)
    try:
        await repository.ensure_funds()
    except Exception as exc:  # noqa: BLE001 - 上游数据源能抛的异常类型无法穷举
        # 预热失败不能挡住启动：第一次请求会再试一次。
        # 但不能静默 —— 冷启动时数据源出问题，这条记录是唯一的线索。
        logger.warning("基金目录预热失败：%s", exc)
    # 排行接口缺的经理/规模/回撤等字段，之前按需补过并落在 Neon 里；
    # 启动时回填进内存，agent 查这些基金就不用重新打逐只接口。
    # （内存为空时 load_enriched 自己会跳过。）
    try:
        from .enrichment import load_enriched

        restored = await load_enriched(repository)
        if restored:
            logger.info("已从快照回填 %s 只基金的补数主数据", restored)
    except Exception as exc:  # noqa: BLE001 - 回填失败等同没有缓存，按需补数会再补
        logger.warning("补数快照回填失败：%s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        print(f"[startup] {await migrate_module.ensure_schema()}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 不挡住服务是刻意的，失败已经打到 stderr
        # 迁移失败必须可见，但不能挡住整个服务：基金数据接口仍可读。
        # 注意这里**不**回退到 create_all —— 迁移失败说明结构变更没落地，
        # 静默降级只会把问题推迟到第一次查询报错。
        print(f"[startup] 数据库 schema 迁移失败：{exc}", file=sys.stderr)
    # 这里只打印掩码：完整邀请码一旦进了日志，就会流向任何能看到运行日志的人，
    # 在云平台上那就是全部项目成员 —— 邀请制当场失效。要抄码请用
    # `scripts/show-invite-code.cmd`，它读的是同一个落盘文件。
    # `/api/auth/policy` 同样永远不回显它，否则等于公开挂在墙上。
    try:
        print(f"[startup] {auth_module.describe_invite_policy()}", file=sys.stderr)
        if auth_module.invite_policy().get("inviteRequired"):
            print("[startup] 完整邀请码请运行 scripts\\show-invite-code.cmd 查看（日志只打掩码）",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 不挡住服务是刻意的，失败已经打到 stderr
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
    except Exception:  # noqa: BLE001 - 兜底中间件，职责就是接住一切未处理异常
        # 必须留日志：不记的话客户端只拿到 500 + requestId，服务端没有任何线索。
        logger.exception("未处理的服务端异常 request_id=%s", request_id)
        response = JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "服务内部错误", "requestId": request_id, "retryable": False}},
        )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # CSP 属于纵深防御，不是补漏：前端零 v-html，本来就没有 XSS 注入点。
    # style-src 必须留 'unsafe-inline' —— 组件里有内联的 style 绑定。
    # connect-src 'self' 够用，因为前后端同源；若将来前端单独托管，要改成后端地址。
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    # HSTS 按 RFC 6797 只在 HTTPS 响应上生效，所以本地跑 http 不会被锁到 https。
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
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


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------
# 认证回答的是「谁能用」，限流回答的是「用多少」。缺了后者有两个真实缺口：
# 服务端 LLM Key 会被任一注册用户无限刷（费用记在部署者账上），
# 登录接口的 24 万次 PBKDF2 迭代可被并发请求打满 CPU。

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def client_ip(request: Request) -> str:
    """取访客真实 IP，用于按来源限流。

    部署形态是 Cloudflare 隧道：源站只接受 cloudflared 发起的回环连接，真实访客
    IP 在 `CF-Connecting-IP` 里。所以**只有对端是回环地址时才采信转发头** ——
    否则任何人都能伪造 `CF-Connecting-IP: 随便填`，让每个请求落进不同的桶，
    等于把限流整个关掉。
    """
    peer = request.client.host if request.client else ""
    if peer in _LOOPBACK_HOSTS:
        forwarded = (request.headers.get("CF-Connecting-IP") or "").strip()
        if not forwarded:
            forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer or "unknown"


def describe_window(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _rate_limited(decision: ratelimit_module.RateLimitDecision, rule: ratelimit_module.RateLimitRule,
                  code: str, action: str) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "code": code,
            "message": f"{action}过于频繁：{describe_window(rule.window_seconds)}内最多 {rule.limit} 次，"
                       f"请 {decision.retry_after} 秒后再试。",
            "retryAfterSeconds": decision.retry_after,
        },
        headers={"Retry-After": str(decision.retry_after)},
    )


def enforce_rate_limit(key: str, rule: ratelimit_module.RateLimitRule, *, code: str, action: str) -> None:
    """消费一次配额，超限即 429。"""
    decision = ratelimit_module.limiter.hit(key, rule)
    if not decision.allowed:
        raise _rate_limited(decision, rule, code, action)


def check_rate_limit(decision: ratelimit_module.RateLimitDecision, rule: ratelimit_module.RateLimitRule,
                     *, code: str, action: str) -> None:
    """只判定不消费。用于「已经超限就别再往下算了」的前置短路。"""
    if not decision.allowed:
        raise _rate_limited(decision, rule, code, action)


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
    # 前端生成的本次研究 ID：服务端收到后立即建立 RUNNING 占位记录，
    # agent 每完成一步工具调用就把 trace 写进去，前端轮询拿到实时思考过程。
    runId: str = Field(default="", max_length=64)


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
    if filters.fundTypes and not any(type_matches(fund.type, filters.fundTypes) for fund in universe):
        available = sorted({re.split(TYPE_SEPARATORS, str(fund.type or ""))[0].strip() for fund in universe if fund.type})
        reasons.append(
            f"类型「{'、'.join(filters.fundTypes)}」在当前数据源中没有匹配项（现有类型：{'、'.join(available) or '无'}）"
        )
    if filters.maxFee and not any(fund.fee is None or fund.fee <= filters.maxFee for fund in universe):
        reasons.append(f"管理费上限 {filters.maxFee}% 没有基金满足")
    if filters.riskLevelMax:
        cap = RISK_RANK.get(filters.riskLevelMax, 99)
        if not any((not is_known(fund.risk)) or RISK_RANK.get(fund.risk, 99) <= cap for fund in universe):
            reasons.append(f"最高风险等级「{filters.riskLevelMax}」下没有基金")
    return ("；".join(reasons) + "。") if reasons else ""


def envelope(items: Any) -> dict[str, Any]:
    source = provider.status.as_dict()
    quality = "REFERENCE" if MODE != "PRODUCTION" else ("ACTIVE" if PRODUCTION_DATA_READY else "UNAVAILABLE")
    return {"items": items, "asOf": datetime.now(UTC).date().isoformat(),
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


RESEARCH_POOL_SIZE = 40


def research_pool(request: ScreenRequest) -> list[Fund]:
    """硬约束照旧；池内排序按与问题的相关性优先，而不是数据源的原始顺序。

    旧行为是取符合硬约束的头 60 只 —— 顺序来自 AKShare 排行，与问题无关：
    问「白酒」时池子里可能一只白酒基金都没有，模型只能在无关基金里硬挑。
    相关性打分走 repository 的 n-gram 索引（中文没有分词，见 providers.py
    的说明），没建索引或打分为 0 时按静态分兜底，保证池子永不为空。
    """
    eligible = [fund for fund in repository.list_funds() if eligibility(fund, request.filters)[0]]
    relevance = repository.relevance_scores(request.query)
    eligible.sort(key=lambda f: (relevance.get(f.code, 0), f.score), reverse=True)
    return eligible[:RESEARCH_POOL_SIZE]


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
async def register(payload: RegisterRequest, request: Request) -> dict[str, Any]:
    """注册并直接返回登录态。口令强度由 security 层统一校验。"""
    # 限流排在最前面：拿脚本批量试邀请码这件事，本就该拦在 PBKDF2 之前。
    enforce_rate_limit(f"rl:register:ip:{client_ip(request)}", ratelimit_module.register_ip_rule(),
                       code="REGISTER_RATE_LIMITED", action="注册")
    # 再按全站兜一道，挡的是换 IP 的分布式尝试。
    enforce_rate_limit("rl:register:global", ratelimit_module.register_global_rule(),
                       code="REGISTER_RATE_LIMITED", action="注册")
    # 邀请码排在建号之前。没通过准入的请求不会进建号逻辑：既省下 PBKDF2 的
    # 24 万次迭代，也避免用 409 告诉对方「这个邮箱已经注册过」。
    if not auth_module.verify_invite_code(payload.inviteCode):
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_INVITE_CODE", "message": "邀请码不正确"})
    try:
        user = await auth_module.create_user(payload.email, payload.password, payload.displayName)
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
async def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
    # 按 IP 限总尝试次数：这是挡「用一个邮箱反复触发热 PBKDF2 打满 CPU」的那一层。
    enforce_rate_limit(f"rl:login:ip:{client_ip(request)}", ratelimit_module.login_ip_rule(),
                       code="LOGIN_RATE_LIMITED", action="登录尝试")
    # 再按账号限失败次数，挡针对已知邮箱的慢速口令爆破。这一层不依赖 IP，
    # 所以伪造转发头绕不过去。
    account_rule = ratelimit_module.login_account_rule()
    account_key = f"rl:login:account:{payload.email.strip().lower()}"
    account_state = ratelimit_module.limiter.peek(account_key, account_rule)

    user = await auth_module.authenticate(payload.email, payload.password)
    if user is not None:
        # 正确凭据永远是通行证，哪怕这个账号此刻正处于「失败过多」状态。
        # 否则任何知道邮箱的人连打几次错密码就能把号主锁在门外 —— 拿限流当
        # 武器比爆破本身廉价得多。放行对攻击者没有好处：他没有正确密码。
        ratelimit_module.limiter.reset(account_key)
        return _session_payload(user)
    if not account_state.allowed:
        raise _rate_limited(account_state, account_rule, "LOGIN_RATE_LIMITED", "账号登录失败")
    ratelimit_module.limiter.hit(account_key, account_rule)
    # 不区分「账号不存在」与「密码错误」，避免账号枚举。
    raise HTTPException(status_code=401, detail={
        "code": "INVALID_CREDENTIALS", "message": "邮箱或密码不正确"})


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
    return


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


BROWSE_LIST_LIMIT = 200


@app.get("/api/funds")
async def funds(
    q: str = Query(default="", max_length=60),
    limit: int = Query(default=0, ge=0, le=100),
    user: UserRecord = Depends(current_user),
) -> dict[str, Any]:
    require_production_data()
    await repository.ensure_funds()
    if q.strip():
        # 按名称/代码搜索：走全量 n-gram 相关性索引（与研究问答同一套打分），
        # 浏览列表只有前 200 只，深层的基金只能靠这里召回。
        scores = repository.relevance_scores(q)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        take = limit or 30
        hits = [repository.funds[code] for code, _ in ranked[:take] if code in repository.funds]
        if q.strip().isdigit():
            # 纯数字按基金代码理解：n-gram 会把 "161725" 的片段错误匹配到
            # 名称含 "2016" 之类的基金，这里把代码前缀命中强制排到最前。
            prefix = q.strip()
            exact = [f for f in repository.funds.values() if f.code == prefix]
            starts = [f for f in repository.funds.values()
                      if f.code.startswith(prefix) and f.code != prefix]
            hits = exact + starts + [f for f in hits if f.code != prefix and not f.code.startswith(prefix)]
        return envelope([fund.as_dict() for fund in hits[:take]])
    # 浏览视图：按静态分取前 200。全集已放开到 AKShare 全量排行（约 2 万只，
    # 供研究与筛选在服务端用），整包发给前端是十几 MB 的 JSON，浏览器吃不消；
    # 深层的基金通过研究问答按相关性召回，不靠翻这个列表。
    browse = sorted(repository.list_funds(), key=lambda f: f.score, reverse=True)
    return envelope([fund.as_dict() for fund in browse[:BROWSE_LIST_LIMIT]])


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
    history: list[dict[str, str]] = []
    if requested_conversation:
        try:
            await auth_module.get_conversation(user.id, requested_conversation)
        except ConversationNotFound as exc:
            raise HTTPException(status_code=404, detail={
                "code": "CONVERSATION_NOT_FOUND", "message": "对话不存在"}) from exc
        # 带上最近几轮对话，追问场景下模型才知道之前聊过什么。本轮 query 尚未
        # 入库，不会和 history 重复；assistant 消息是整段研究摘要，截断到 600 字。
        prior = await auth_module.list_messages(user.id, requested_conversation, limit=200)
        for message in prior[-6:]:
            role = message.get("role")
            content = str(message.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                history.append({"role": role, "content": content[:600]})
    # 服务端 API Key 是共享钱包，所以配额必须在调用模型**之前**原子占位：
    # 若等到跑完再统计，并发请求早已一起通过了「还剩 N 次」的检查，额度会被一次刷爆。
    # 先 peek 一遍只是为了让已经超限的请求立刻被拒，不白跑一次知识库检索。
    quota = [
        (f"rl:research:user:{user.id}:hour", ratelimit_module.research_user_hourly_rule()),
        (f"rl:research:user:{user.id}:day", ratelimit_module.research_user_daily_rule()),
        ("rl:research:global:day", ratelimit_module.research_global_daily_rule()),
    ]
    for key, rule in quota:
        check_rate_limit(ratelimit_module.limiter.peek(key, rule), rule,
                         code="RESEARCH_QUOTA_EXCEEDED", action="研究调用")
    reserved: list[tuple[str, ratelimit_module.RateLimitRule]] = []
    for key, rule in quota:
        decision = ratelimit_module.limiter.hit(key, rule)
        if not decision.allowed:
            # 占位做到一半失败，把已占的部分退回去，别留下半次扣费。
            for taken_key, taken_rule in reserved:
                ratelimit_module.limiter.refund(taken_key, taken_rule)
            raise _rate_limited(decision, rule, "RESEARCH_QUOTA_EXCEEDED", "研究调用")
        reserved.append((key, rule))
    knowledge = await knowledge_base.search(request.query, limit=5)
    research_llm = llm_service
    llm_succeeded = False
    agent_used = False
    agent_trace: list[dict[str, str]] = []
    # RUNNING 占位：请求带 runId 时先落一条内存记录（注意：这不是数据库落库，
    # 只服务于研究页的实时过程展示；研究失败时它会被标记为 FAILED）。
    requested_run_id = request.runId.strip()
    placeholder: dict[str, Any] | None = None
    if requested_run_id:
        placeholder = {
            "runId": requested_run_id, "status": "RUNNING", "mode": f"{MODE}_RESEARCH",
            "originalQuery": request.query, "createdAt": datetime.now(UTC).isoformat(),
            "policyVersion": "research-policy-v1", "modelVersion": f"{llm_service.provider}:{llm_service.model}",
            "snapshotId": f"{MODE.lower()}-snapshot",
            "trace": [
                {"event": "plan_created", "title": "理解研究目标", "detail": request.query[:200], "status": "COMPLETED"},
                {"event": "knowledge_retrieved", "title": "检索研究知识库", "detail": f"检索到 {len(knowledge)} 个可引用片段。", "status": "COMPLETED"},
                {"event": "tool_completed", "title": "圈定候选池", "detail": f"从 {MODE} AKShare 数据快照筛得 {len(items)} 只符合硬约束的候选。{unverified_note(items, request.filters)}", "status": "COMPLETED"},
                {"event": "agent_started", "title": "启动 Agent 工具循环", "detail": "模型开始自主调用工具取证。", "status": "RUNNING"},
            ],
        }
        repository.runs[requested_run_id] = placeholder
    try:
        if request.llm is not None or not agent_research.enabled:
            # 会话级自定义端点不保证支持 function calling，一律走旧单轮链路。
            if request.llm is not None:
                research_llm = llm_service.for_session_request(request.llm.model_dump(exclude_none=True))
            model_output = await research_llm.research(request.query, items, knowledge, request.limit, history=history)
        else:
            # Agent 模式：模型自主调工具取真实数据。失败时降级到旧链路重跑，
            # 而不是直接报错 —— agent 可能已消耗数轮模型调用，降级是最后一搏。
            def _push_trace(step: dict[str, str]) -> None:
                if placeholder is not None:
                    # 工具开始事件到达时，把上一条还在 RUNNING 的标记为已完成，
                    # 避免前端看到两条同时"进行中"的步骤。
                    for existing in placeholder["trace"]:
                        if existing.get("status") == "RUNNING":
                            existing["status"] = "COMPLETED"
                    placeholder["trace"].append(step)

            try:
                model_output, agent_trace = await agent_research.run_research(
                    request.query, history, request.limit,
                    pool=[LLMService._compact_fund(fund) for fund in items],
                    on_trace=_push_trace,
                )
                research_llm = agent_research
                agent_used = True
            except LLMError:
                model_output = await research_llm.research(request.query, items, knowledge, request.limit, history=history)
        llm_succeeded = True
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_LLM_CONFIGURATION", "message": str(exc)}) from exc
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail={"code": "LLM_NOT_CONFIGURED", "message": str(exc)}) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail={"code": "LLM_RESPONSE_INVALID", "message": str(exc)}) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail={"code": "LLM_UNAVAILABLE", "message": str(exc)}) from exc
    finally:
        # 模型没跑成就退还占位：Key 未配置、上游报错、配置非法都不该白扣用户次数。
        if not llm_succeeded:
            for key, rule in reserved:
                ratelimit_module.limiter.refund(key, rule)
            if placeholder is not None:
                placeholder["status"] = "FAILED"
                placeholder["trace"].append({
                    "event": "llm_failed", "title": "研究未能完成",
                    "detail": "模型调用失败，已停止本次研究。可稍后重试。",
                    "status": "FAILED",
                })
    # 对话与消息在 run 载荷完全构建成功后才落库（见函数末尾）：此前 user 消息
    # 先写、assistant 后写，中间构建载荷一旦抛异常就留下只有用户提问的孤儿
    # 记录（复盘日志里删之不甘、留之无用）。整体后置让两步写入要么都发生、
    # 要么都不发生。
    by_code = {item.code: item for item in items}
    ranked_funds: list[tuple[Fund, Any]] = []
    for assessment in model_output.ranking:
        original = by_code.get(assessment.fundCode)
        if not original:
            # Agent 模式下 ranking 可以引用工具检索到的池外基金（valid_codes 已
            # 校验代码来自工具真实返回），这里从全量数据回源补齐，否则"美股"
            # 这类预筛池覆盖不了的问题会算出 0 个候选。
            original = repository.get_fund(assessment.fundCode)
        if not original:
            continue
        ranked_funds.append((Fund(**{**original.__dict__, "score": assessment.score,
                                     "reason": assessment.reason, "highlights": [assessment.fit],
                                     "caveat": "；".join(assessment.riskFlags) or original.caveat}), assessment))
    run_id = requested_run_id or f"run_{uuid.uuid4().hex[:12]}"
    static_trace = [{"event": "plan_created", "title": "理解研究目标", "detail": model_output.intent, "status": "COMPLETED"},
                    {"event": "knowledge_retrieved", "title": "检索研究知识库", "detail": f"检索到 {len(knowledge)} 个可引用片段。", "status": "COMPLETED"},
                    {"event": "tool_completed", "title": "检索真实候选", "detail": f"从 {MODE} AKShare 数据快照取得 {len(items)} 只符合硬约束的候选。{unverified_note(items, request.filters)}", "status": "COMPLETED"}]
    if agent_used:
        static_trace.append({"event": "agent_started", "title": "启动 Agent 工具循环",
                             "detail": f"模型自主调用工具取证，共 {len(agent_trace)} 次工具调用。", "status": "COMPLETED"})
        static_trace.extend(agent_trace)
    static_trace.append({"event": "llm_completed", "title": "大模型生成研究结论", "detail": "模型输出已按候选代码和结构化 Schema 校验。", "status": "COMPLETED"})
    run = {"runId": run_id, "status": "COMPLETED", "mode": f"{MODE}_{'AGENT' if agent_used else 'LLM'}_RESEARCH",
           "originalQuery": request.query, "createdAt": datetime.now(UTC).isoformat(),
           "completedAt": datetime.now(UTC).isoformat(), "policyVersion": "research-policy-v1",
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
           "trace": static_trace,
           "disclaimer": "以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。"
                         "数据可能存在延迟或缺漏，以基金管理人官方披露为准。"
                         "市场有风险，投资需谨慎；任何投资决策应结合个人风险承受能力、"
                         "资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。", "nextQuestions": model_output.followUpQuestions}
    conversation_id = requested_conversation or (await auth_module.create_conversation(user.id, request.query))["id"]
    await auth_module.append_message(
        user.id, conversation_id, "user", request.query,
        payload={"filters": request.filters.model_dump(), "limit": request.limit},
    )
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
            "confirmedAt": datetime.now(UTC).isoformat(), "questionnaireVersion": "risk-questionnaire-v1"}


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
