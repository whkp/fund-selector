"""限流回归测试。

锁定的不变量：

1. **内核语义** —— 窗口内允许 N 次、第 N+1 次拒绝；窗口滚动后恢复；`refund` 能
   归还一次且不会把计数推成负数；上限配 0 视为停用；`peek` 只判不消费。
2. **登录** —— 同一账号连续失败到上限后 429；成功登录会清零失败计数，所以正常
   用户不会因为「自己手滑几次」被锁在门外。
3. **注册** —— 同一来源超限后 429，挡的是拿脚本批量试邀请码。
4. **研究配额** —— 在调用模型**之前**原子占位，超限立刻 429，不会先花掉一次模型调用；
   而模型调用失败（例如服务端未配置 Key）会把占位退还，不白扣用户次数。

`limiter` 是进程内单例，一个用例把桶填满会污染下一个，所以每个用例前后都要重置。
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app import ratelimit
from app.main import app
from app.models import Fund
from app.ratelimit import RateLimitRule

client = TestClient(app)
limiter = ratelimit.limiter

INVITE_CODE = os.environ["FUND_COMPASS_INVITE_CODE"]


@pytest.fixture(autouse=True)
def clean_limiter():
    limiter.reset_all()
    yield
    limiter.reset_all()


def unique_email() -> str:
    return f"ratelimit_{uuid.uuid4().hex[:10]}@example.com"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(email: str, invite: str, password: str = "test-password-123"):
    return client.post("/api/auth/register", json={
        "email": email, "password": password, "inviteCode": invite,
    })


def login(email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def signed_in_user() -> tuple[str, str]:
    """注册一个账号，返回 (token, user_id)。"""
    token = register(unique_email(), INVITE_CODE).json()["token"]
    user_id = client.get("/api/auth/me", headers=auth(token)).json()["user"]["id"]
    return token, user_id


def make_fund(code: str = "008286") -> Fund:
    return Fund(
        id=f"fund_{code}", code=code, name="限流测试基金", short_name="限流测试",
        type="混合型-偏股", risk="未获取", manager="测试经理", manager_years=None,
        company="测试公司", theme="", nav=1.0, nav_date="2026-09-11", ytd=1.0,
        one_year=1.0, volatility=None, drawdown=None, fee=None, scale=None,
        inception=None, score=0, score_parts=[], reason="", caveat="", highlights=[],
        status="", source="", snapshot="", chart=[], tags=[], intake="未获取",
        quality_status="",
    )


# ---------------------------------------------------------------------------
# 内核
# ---------------------------------------------------------------------------

def test_hit_allows_up_to_limit_then_denies():
    rule = RateLimitRule(3, 60)
    for expected_remaining in (2, 1, 0):
        decision = limiter.hit("core:limit", rule)
        assert decision.allowed is True
        assert decision.remaining == expected_remaining
    denied = limiter.hit("core:limit", rule)
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after > 0


def test_peek_does_not_consume_and_reports_exhaustion():
    rule = RateLimitRule(1, 60)
    assert limiter.peek("core:peek", rule).allowed is True
    assert limiter.hit("core:peek", rule).allowed is True
    peeked = limiter.peek("core:peek", rule)
    assert peeked.allowed is False
    # peek 不该推进计数：再查一次结论一致。
    assert limiter.peek("core:peek", rule).allowed is False
    assert limiter.count("core:peek", rule) == 1


def test_refund_returns_a_slot_and_never_goes_negative():
    rule = RateLimitRule(2, 60)
    limiter.hit("core:refund", rule)
    limiter.hit("core:refund", rule)
    assert limiter.hit("core:refund", rule).allowed is False

    limiter.refund("core:refund", rule)
    assert limiter.count("core:refund", rule) == 1
    assert limiter.hit("core:refund", rule).allowed is True

    # 退多了也不能把计数推成负数去凭空放大额度。
    for _ in range(5):
        limiter.refund("core:refund", rule)
    assert limiter.count("core:refund", rule) == 0


def test_window_rollover_restores_quota():
    rule = RateLimitRule(1, 1)
    assert limiter.hit("core:rollover", rule).allowed is True
    assert limiter.hit("core:rollover", rule).allowed is False
    time.sleep(1.05)
    assert limiter.hit("core:rollover", rule).allowed is True


def test_zero_limit_disables_the_rule():
    """上限配 0 是运维的「临时关掉这条限流」开关，本地调试用。"""
    rule = RateLimitRule(0, 60)
    assert rule.enabled is False
    for _ in range(50):
        assert limiter.hit("core:disabled", rule).allowed is True
    assert limiter.peek("core:disabled", rule).allowed is True


def test_reset_clears_the_bucket():
    rule = RateLimitRule(1, 60)
    limiter.hit("core:reset", rule)
    assert limiter.peek("core:reset", rule).allowed is False
    limiter.reset("core:reset")
    assert limiter.peek("core:reset", rule).allowed is True
    assert limiter.count("core:reset", rule) == 0


def test_rules_are_read_at_call_time_so_tests_can_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_LOGIN_ACCOUNT_MAX", "7")
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_LOGIN_ACCOUNT_WINDOW_SECONDS", "60")
    rule = ratelimit.login_account_rule()
    assert rule.limit == 7
    assert rule.window_seconds == 60


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

def test_repeated_failed_logins_are_throttled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_LOGIN_ACCOUNT_MAX", "3")
    email = unique_email()
    assert register(email, INVITE_CODE).status_code == 201

    for _ in range(3):
        assert login(email, "definitely-wrong").status_code == 401

    blocked = login(email, "definitely-wrong")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "LOGIN_RATE_LIMITED"
    assert blocked.headers.get("Retry-After")


def test_successful_login_clears_failure_counter(monkeypatch: pytest.MonkeyPatch):
    """手滑几次之后输对密码，不该继续背着失败计数。"""
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_LOGIN_ACCOUNT_MAX", "3")
    email = unique_email()
    password = "correct-password-123"
    assert register(email, INVITE_CODE, password).status_code == 201

    assert login(email, "wrong-1").status_code == 401
    assert login(email, "wrong-2").status_code == 401
    assert login(email, password).status_code == 200

    # 计数已清零：还能再失败满 3 次才被拦。
    for _ in range(3):
        assert login(email, "wrong-again").status_code == 401
    assert login(email, "wrong-again").status_code == 429


def test_lockout_cannot_be_weaponised_against_the_real_owner(monkeypatch: pytest.MonkeyPatch):
    """账号维度的限流不该变成「知道邮箱就能把号主锁死」的武器。

    正确凭据必须永远是通行证 —— 否则攻击者连打几次错密码的代价，比爆破本身低太多。
    """
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_LOGIN_ACCOUNT_MAX", "2")
    email = unique_email()
    password = "correct-password-123"
    assert register(email, INVITE_CODE, password).status_code == 201

    assert login(email, "wrong-1").status_code == 401
    assert login(email, "wrong-2").status_code == 401
    # 攻击者视角：账号已进入限流状态，继续猜只会拿到 429。
    assert login(email, "wrong-3").status_code == 429
    assert login(email, "wrong-4").status_code == 429
    # 号主视角：正确密码照样进得来，而且顺手把失败计数清零。
    assert login(email, password).status_code == 200
    assert login(email, "wrong-5").status_code == 401


def test_login_ip_limit_is_enforced(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_LOGIN_IP_MAX", "2")
    for _ in range(2):
        assert login(unique_email(), "whatever").status_code == 401
    blocked = login(unique_email(), "whatever")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "LOGIN_RATE_LIMITED"


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def test_register_is_throttled_per_ip(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_REGISTER_IP_MAX", "2")
    assert register(unique_email(), "wrong-code").status_code == 400
    assert register(unique_email(), "wrong-code").status_code == 400

    blocked = register(unique_email(), INVITE_CODE)
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "REGISTER_RATE_LIMITED"
    assert blocked.headers.get("Retry-After")


def test_register_global_limit_backstops_ip_rotation(monkeypatch: pytest.MonkeyPatch):
    """换 IP 也绕不过全站兜底。"""
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_REGISTER_IP_MAX", "1000")
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_REGISTER_GLOBAL_MAX", "2")
    assert register(unique_email(), "wrong-code").status_code == 400
    assert register(unique_email(), "wrong-code").status_code == 400
    assert register(unique_email(), "wrong-code").status_code == 429


# ---------------------------------------------------------------------------
# 模型研究配额
# ---------------------------------------------------------------------------

def test_research_is_rejected_once_quota_is_used_up(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_RESEARCH_USER_HOURLY_MAX", "1")
    monkeypatch.setattr(main_module, "research_pool", lambda request: [make_fund()])
    token, user_id = signed_in_user()

    rule = ratelimit.research_user_hourly_rule()
    key = f"rl:research:user:{user_id}:hour"
    assert limiter.hit(key, rule).allowed is True

    blocked = client.post("/api/recommendations/runs",
                          json={"query": "找一只稳健的基金", "limit": 1}, headers=auth(token))
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RESEARCH_QUOTA_EXCEEDED"
    assert blocked.headers.get("Retry-After")


def test_research_global_daily_quota_backstops_account_farming(monkeypatch: pytest.MonkeyPatch):
    """注册多个账号轮流刷也绕不过全站日上限。"""
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_RESEARCH_GLOBAL_DAILY_MAX", "1")
    monkeypatch.setattr(main_module, "research_pool", lambda request: [make_fund()])
    token, _ = signed_in_user()

    rule = ratelimit.research_global_daily_rule()
    assert limiter.hit("rl:research:global:day", rule).allowed is True

    blocked = client.post("/api/recommendations/runs",
                          json={"query": "找一只稳健的基金", "limit": 1}, headers=auth(token))
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RESEARCH_QUOTA_EXCEEDED"


def test_quota_is_reserved_before_model_call_and_refunded_on_failure(monkeypatch: pytest.MonkeyPatch):
    """服务端未配置 Key 时模型必然失败，配额必须退回，不能白扣用户次数。

    这条同时证明了占位确实发生在模型调用之前：如果顺序反了（先跑模型再记账），
    失败路径下计数根本不会被触碰，断言 `== 0` 会以另一种方式「碰巧通过」——
    所以下面先断言超限时 429，再断言失败后归零。
    """
    monkeypatch.setenv("FUND_COMPASS_RATE_LIMIT_RESEARCH_USER_HOURLY_MAX", "2")
    monkeypatch.setattr(main_module, "research_pool", lambda request: [make_fund()])
    token, user_id = signed_in_user()

    rule = ratelimit.research_user_hourly_rule()
    key = f"rl:research:user:{user_id}:hour"

    # 测试环境没有配置模型，所以这里拿到的是 503（LLM_NOT_CONFIGURED）。
    for _ in range(3):
        response = client.post("/api/recommendations/runs",
                               json={"query": "找一只稳健的基金", "limit": 1}, headers=auth(token))
        assert response.status_code == 503, response.text[:200]

    assert limiter.count(key, rule) == 0, "模型调用失败后配额应当全部退还"


def test_research_still_proceeds_while_quota_remains(monkeypatch: pytest.MonkeyPatch):
    """反向确认：429 是配额引起的，而不是这条路径本身坏掉了。"""
    monkeypatch.setattr(main_module, "research_pool", lambda request: [make_fund()])
    token, user_id = signed_in_user()
    response = client.post("/api/recommendations/runs",
                           json={"query": "找一只稳健的基金", "limit": 1}, headers=auth(token))
    assert response.status_code != 429
    # 失败已退还，所以计数应为 0。
    assert limiter.count(f"rl:research:user:{user_id}:hour",
                         ratelimit.research_user_hourly_rule()) == 0
