"""Pytest 全局配置。

认证测试必须打真实数据库，但绝不能用开发库 —— 这里在**导入任何 app 模块之前**
把数据库指向临时文件，并把 AKShare 关掉，保证测试可重复、不触网、不污染
`backend/data/fund-compass.db`。

`conftest.py` 由 pytest 最先加载，所以下面的环境变量会先于各测试模块顶部的
`import app.main` 生效。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="fund-compass-tests-"))
_DB_PATH = (_TMP_DIR / "test-auth.db").as_posix()

os.environ["FUND_COMPASS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ["FUND_COMPASS_AKSHARE_ENABLED"] = "false"
os.environ["FUND_COMPASS_MODE"] = "REFERENCE"
os.environ["FUND_COMPASS_JWT_SECRET"] = "test-secret-not-for-production"
# 固定一个已知邀请码：注册类用例需要它，而「邀请码关卡」本身也必须有确定的
# 期望值。不设的话会走「自动生成并落盘」分支，测试结果就依赖磁盘上残留的文件。
os.environ["FUND_COMPASS_INVITE_CODE"] = "test-invite-code"

# LLM 必须始终保持「未配置」：测试绝不允许调用真实模型（会花钱、依赖网络、
# 结果不确定）。2026-09-19 加了本地 config/fund-compass.json 之后，
# `test_rate_limit.py` 的两条用例当场变成真调用并返回 200，前提被判据打穿。
# 注意 config_value() 对**空字符串**会回退到配置文件，所以这里不能设空串，
# 必须删掉变量 + 把配置文件指向不存在的路径。
for _llm_env in (
    "FUND_COMPASS_LLM_PROVIDER", "FUND_COMPASS_LLM_MODEL", "FUND_COMPASS_LLM_BASE_URL",
    "FUND_COMPASS_LLM_API_KEY", "FUND_COMPASS_LLM_TIMEOUT_SECONDS",
):
    os.environ.pop(_llm_env, None)
os.environ["FUND_COMPASS_CONFIG_FILE"] = str(_TMP_DIR / "no-such-config.json")

# 限流默认值是按「真人手工点几下」定的，但测试会在同一个进程里连着注册几十个
# 账号、反复登录，而且全部来自同一个回环 IP —— 不放宽的话既有用例会先撞上 429，
# 失败原因看起来还和被测逻辑无关。限流本身的行为由 tests/test_rate_limit.py
# 用显式的小上限单独验证，那边每个用例都会重置计数器。
for _rate_limit_name in (
    "LOGIN_IP", "LOGIN_ACCOUNT", "REGISTER_IP", "REGISTER_GLOBAL",
    "RESEARCH_USER_HOURLY", "RESEARCH_USER_DAILY", "RESEARCH_GLOBAL_DAILY",
):
    os.environ[f"FUND_COMPASS_RATE_LIMIT_{_rate_limit_name}_MAX"] = "100000"

import pytest  # noqa: E402


def _reset_engine() -> None:
    import app.db.session as session_module

    session_module._engine = None
    session_module._session_factory = None


@pytest.fixture(scope="session", autouse=True)
def prepare_schema():
    """建一次表。TestClient 不进入 lifespan，所以不能依赖应用启动钩子。"""
    import asyncio

    from app.auth import ensure_auth_schema
    from app.db.session import get_engine

    async def create() -> None:
        await ensure_auth_schema()
        # 建表用的 event loop 马上要结束，先释放连接池，避免连接被跨 loop 复用。
        await get_engine().dispose()

    asyncio.run(create())
    yield


@pytest.fixture(autouse=True)
def isolate_database_engine():
    """每个测试都从干净的连接池开始。

    `test_db.py` 会 monkeypatch 数据库地址并重置 engine 单例；如果不在这里
    复位，后面的测试会继续连着上一个测试的临时库。
    """
    _reset_engine()
    yield
    _reset_engine()
