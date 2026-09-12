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
