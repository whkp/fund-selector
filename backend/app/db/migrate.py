"""启动时把数据库 schema 推到最新版本。

**Alembic 是唯一权威。** 部署链路不能再依赖 `Base.metadata.create_all`
去"顺手补上"新的表和列 —— 它只建**缺失的表**，从不 ALTER **已存在**的表。
后果是：本地新建库跑测试全绿，线上老库结构静默落后，直到某个查询报
`no such column` 才暴露。这是本项目最隐蔽的一类故障。

只有迁移环境不完整时才回退到 `create_all`：`scripts/publish-hf-space.sh`
会把 `alembic.ini` 和 `migrations/` 从 Space 产物里删掉（Space 只跑应用，
不带开发期文件），那条路径上没有迁移可用。回退保证服务仍能起来，但会返回
一条显眼的警告，不至于被当成"迁移正常执行了"。

关于老库：本仓库的 `backend/data/fund-compass.db` 是历史上用 `create_all`
建起来的，没有 `alembic_version` 表。直接 upgrade 是安全的 —— 两个迁移都
是**幂等**设计：`0001` 走 `create_all`（跳过已存在的表），`0002` 逐列判断
是否存在再 `add_column`。首次 upgrade 会补齐缺失结构并写入版本号。

并发说明：迁移在进程启动时执行一次。当前所有部署形态都是单进程
（`backend/run.py` 不传 `--workers`，Dockerfile 的 CMD 同样是单进程），
不存在两个 worker 同时 upgrade 的竞争。将来若上多 worker，必须把这一步
移出应用启动、改成部署前置步骤（Render 的 `preDeployCommand`），否则升级
可能交错执行。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
MIGRATIONS_DIR = BACKEND_DIR / "migrations"


def migration_environment_available() -> bool:
    """迁移所需的两样东西是否都在。"""
    return ALEMBIC_INI.is_file() and MIGRATIONS_DIR.is_dir()


def _build_config():
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    # 一律用绝对路径，避免依赖启动时的 cwd（容器里是 /app，本地是仓库根）。
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # env.py 需要 `from app.config import ...`；显式指定绝对路径，
    # 就不必指望调用方已经把 backend/ 放进 sys.path 或 cwd。
    config.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    return config


def _upgrade_sync() -> str:
    """同步执行 `alembic upgrade head`，返回目标 revision。

    **必须在没有运行中事件循环的线程里调用**：`migrations/env.py` 的
    `run_migrations_online()` 内部会 `asyncio.run()`，在已有 loop 的线程里
    直接调用会抛 "asyncio.run() cannot be called from a running event loop"。
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    config = _build_config()
    head = ScriptDirectory.from_config(config).get_current_head()
    command.upgrade(config, "head")
    return head or "unknown"


async def ensure_schema() -> str:
    """把 schema 推到 head，返回一行可直接打印的说明。"""
    # 先触发一次 database_url()：它对 sqlite 路径带 mkdir 副作用，保证库文件
    # 所在目录存在。迁移自己在 env.py 里建 engine，**不会**建目录 —— 容器首次
    # 启动时 /app/data/ 并不存在，缺这一步会直接 "unable to open database file"。
    from .session import database_url

    database_url()

    if not migration_environment_available():
        from ..auth import ensure_auth_schema

        await ensure_auth_schema()
        return (
            "未找到 alembic.ini 或 migrations/，已回退到 create_all；"
            "本次不会 ALTER 已存在的表，结构变更需在有迁移的部署里生效"
        )
    head = await asyncio.to_thread(_upgrade_sync)
    return f"数据库 schema 已迁移到 {head}"
