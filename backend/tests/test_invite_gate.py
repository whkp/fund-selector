"""邀请码准入，以及「未登录不能进入系统」的回归测试。

锁定的不变量：

1. 邀请码缺失或错误时注册必须失败；且这个判断发生在建号之前，所以
   「邮箱是否已注册」不会从响应码里泄漏给没有准入资格的人。
2. 邀请码比对容忍空白 / 大小写 / 连字符差异，但不接受任何近似值。
3. `/api/auth/policy` 可匿名访问，且**永远不回显邀请码本身**。
4. 未登录访问任何数据接口都必须 401。前端门禁只是界面层的礼貌，
   真正的边界在服务端 —— 这一条如果松了，整套登录就形同虚设。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.main import app
from app.security import match_invite_code, normalize_invite_code

client = TestClient(app)

# 由 conftest 固定注入，见 tests/conftest.py。
INVITE_CODE = os.environ["FUND_COMPASS_INVITE_CODE"]


def unique_email() -> str:
    return f"invite_{uuid.uuid4().hex[:10]}@example.com"


def register(email: str, invite: str | None):
    """`invite=None` 表示请求里完全不出现 inviteCode 字段。"""
    payload: dict[str, str] = {"email": email, "password": "test-password-123"}
    if invite is not None:
        payload["inviteCode"] = invite
    return client.post("/api/auth/register", json=payload)


# ---------------------------------------------------------------------------
# 归一化与恒定时间比对
# ---------------------------------------------------------------------------

def test_invite_code_normalization_ignores_formatting_noise():
    assert normalize_invite_code("  AbCd-EfGh  ") == "abcdefgh"
    assert normalize_invite_code("abcd_efgh") == "abcdefgh"
    assert normalize_invite_code("") == ""
    assert normalize_invite_code(None) == ""  # type: ignore[arg-type]


def test_match_invite_code_accepts_reformatted_input_but_nothing_close():
    # 夹具必须是杜撰值：本文件会进公开仓库，绝不能出现任何真实邀请码。
    codes = ["k7qm2xr9tlpz4nwd"]
    assert match_invite_code("k7qm2xr9tlpz4nwd", codes) is True
    assert match_invite_code("  K7QM2XR9TLPZ4NWD ", codes) is True
    assert match_invite_code("k7qm-2xr9-tlpz-4nwd", codes) is True
    # 少一位、改一位、空值都不行。
    assert match_invite_code("k7qm2xr9tlpz4nw", codes) is False
    assert match_invite_code("k7qm2xr9tlpz4nwe", codes) is False
    assert match_invite_code("", codes) is False
    # 空集合表示「开放注册」，由 verify_invite_code 处理，这里必须返回 False。
    assert match_invite_code("anything", []) is False


# ---------------------------------------------------------------------------
# 注册关卡
# ---------------------------------------------------------------------------

def test_register_rejects_missing_and_wrong_invite_code():
    missing = register(unique_email(), None)
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "INVALID_INVITE_CODE"

    wrong = register(unique_email(), "definitely-not-the-code")
    assert wrong.status_code == 400
    assert wrong.json()["detail"]["code"] == "INVALID_INVITE_CODE"

    ok = register(unique_email(), INVITE_CODE)
    assert ok.status_code == 201
    assert ok.json()["token"]


def test_register_accepts_reformatted_invite_code():
    """人肉转发邀请码时经常带上空格或大小写变化，不该因此被拒。"""
    ok = register(unique_email(), f"  {INVITE_CODE.upper()}  ")
    assert ok.status_code == 201


def test_invite_code_is_checked_before_account_enumeration():
    """没有邀请码的人不该从 409 里学到「这个邮箱已经注册过」。"""
    email = unique_email()
    assert register(email, INVITE_CODE).status_code == 201

    blocked = register(email, "wrong-code")
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "INVALID_INVITE_CODE"


def test_rejected_registration_leaves_no_account_behind():
    email = unique_email()
    assert register(email, "wrong-code").status_code == 400
    # 如果上一次偷偷建了号，这里会撞 409 而不是成功。
    assert register(email, INVITE_CODE).status_code == 201


# ---------------------------------------------------------------------------
# 策略端点
# ---------------------------------------------------------------------------

def test_policy_endpoint_is_public_and_never_echoes_the_code():
    response = client.get("/api/auth/policy")
    assert response.status_code == 200
    body = response.json()
    assert body["inviteRequired"] is True
    # 关键：响应里不能出现码本身，也不该出现任何能反推码的字段。
    assert INVITE_CODE not in response.text
    assert set(body) == {"inviteRequired", "inviteCodeCount"}


def test_multiple_invite_codes_can_be_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_COMPASS_INVITE_CODE", "code-one, code-two;code-three")
    assert auth_module.invite_policy()["inviteCodeCount"] == 3
    for code in ("code-one", "code-two", "code-three"):
        assert register(unique_email(), code).status_code == 201
    assert auth_module.verify_invite_code("code-four") is False


def test_explicit_disable_opens_registration(monkeypatch: pytest.MonkeyPatch):
    """显式配成 off 才开放注册 —— 默认不配置是封闭，不是开放。"""
    monkeypatch.setenv("FUND_COMPASS_INVITE_CODE", "off")
    assert auth_module.invite_policy() == {"inviteRequired": False, "inviteCodeCount": 0}
    assert register(unique_email(), None).status_code == 201
    assert register(unique_email(), "whatever-was-typed").status_code == 201


# ---------------------------------------------------------------------------
# 全站封闭：未登录不得进入
# ---------------------------------------------------------------------------

PROTECTED_GETS = [
    "/api/funds",
    "/api/funds/008286",
    "/api/funds/008286/history",
    "/api/funds/008286/data-quality",
    "/api/ai/status",
    "/api/knowledge/status",
    "/api/market/etf-quotes",
    "/api/data-sources/status",
    "/api/profile/risk",
    "/api/watchlist/items",
    "/api/conversations",
    "/api/auth/me",
]

PROTECTED_POSTS = [
    ("/api/funds/screen", {"query": "x", "limit": 1}),
    ("/api/funds/compare", {"codes": ["008286"]}),
    ("/api/knowledge/search", {"query": "test"}),
    ("/api/market/etf-quotes/refresh", None),
    ("/api/data/funds/refresh", None),
    ("/api/recommendations/runs", {"query": "x", "limit": 1}),
    ("/api/conversations", {"title": "x"}),
]


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_anonymous_get_is_rejected(path: str):
    response = client.get(path)
    assert response.status_code == 401, f"{path} -> {response.status_code} {response.text[:120]}"
    assert response.json()["detail"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("path,body", PROTECTED_POSTS)
def test_anonymous_post_is_rejected(path: str, body):
    response = client.post(path, json=body)
    assert response.status_code == 401, f"{path} -> {response.status_code} {response.text[:120]}"


@pytest.mark.parametrize("path", ["/health", "/ready", "/api/auth/policy"])
def test_operational_and_entry_paths_stay_public(path: str):
    """探针和登录入口必须保持匿名可达，否则隧道健康检查和登录页都会挂。"""
    assert client.get(path).status_code == 200


def test_authenticated_user_gets_through_the_same_paths():
    """反向确认：401 是「没登录」造成的，而不是这些路径本身坏掉了。"""
    headers = {"Authorization": f"Bearer {register(unique_email(), INVITE_CODE).json()['token']}"}
    for path in PROTECTED_GETS:
        if path == "/api/auth/me":
            continue
        response = client.get(path, headers=headers)
        # 008286 这类基金在当前测试语料里可能不存在，但绝不该是 401/403。
        assert response.status_code not in (401, 403), f"{path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# 回归防线：真实邀请码不得进入版本库
# ---------------------------------------------------------------------------

def test_live_invite_code_never_leaks_into_tracked_source():
    """真实邀请码只该待在 backend/data/.invite-code（已被 .gitignore 覆盖）。

    这条防线是被真实事故催生的：曾经把生产邀请码手抄进本文件的断言当夹具，
    随提交进了 git 历史 —— 只差一次 push 就公开了。夹具一律用杜撰值。
    """
    live = ""
    try:
        if auth_module.DEFAULT_INVITE_FILE.exists():
            live = auth_module.DEFAULT_INVITE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pytest.skip("邀请码文件不可读")
    if not live:
        pytest.skip("尚未生成邀请码，无需检查")

    root = Path(__file__).resolve().parents[2]
    scanned_suffixes = {".py", ".ts", ".vue", ".json", ".cmd", ".sh", ".md", ".yaml", ".yml", ".html"}
    scan_roots = [root / "backend" / "app", root / "backend" / "tests", root / "src", root / "scripts", root / "config"]

    offenders: list[str] = []
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for path in scan_root.rglob("*"):
            if not path.is_file() or path.suffix not in scanned_suffixes:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if live in text:
                offenders.append(str(path.relative_to(root)))

    assert not offenders, f"真实邀请码出现在会被提交的文件里：{offenders}"
