# 基金罗盘 MVP

基于 [项目设计文档](docs/基金罗盘-项目设计文档.md) 实现的本地全栈 MVP，用于基金研究与筛选，不包含交易、收益预测或个性化投资建议。

当前技术栈为 Vue 3 + TypeScript 前端、FastAPI/Python 后端、AKShare 开发阶段数据入口。进展见 [开发进展](docs/开发进展.md)，来源边界见 [数据更新与来源](docs/数据更新与来源.md)。

## 包含内容

- 自然语言研究目标和可编辑筛选条件
- 基金候选、匹配理由、风险提示、数据日期和质量状态
- 基金公开参考排序、基金详情和历史 NAV 曲线，均由 AKShare 按请求获取
- 最多 4 只基金的同口径对比
- 本地观察列表、备注和推荐 Trace 演示
- FastAPI 的基金、筛选、对比、风险画像、观察列表、数据质量与推荐 Trace 接口
- 服务端确定性约束：风险等级、费率、开放申购状态与数据质量校验
- AKShare 基金目录/开放式基金排行参考入口（Reference 模式启用）
- Python 历史净值指标计算基础：收益、年化收益、波动率、最大回撤和夏普
- 服务端 OpenAI-compatible/Ollama 大模型研究；模型按真实候选数据生成结构化结论，未配置时明确报错
- 可选本地 Markdown 知识库接口，后续可替换为向量数据库或外部检索服务
- 公网短期 BYOK：用户可在页面设置本次会话模型，Key 只存在页面内存，不落库、不进入研究记录

系统不再提供基金 seed 或浏览器 Mock 回退。AKShare 结果标记为公开聚合参考，不能替代授权生产数据。普通基金正式 NAV、ETF/LOF 场内行情和衍生指标必须分字段展示。

## 运行

环境要求：Node.js 20+、Python 3.13（精确版本见仓库根 `.python-version`，与 `Dockerfile` 一致）。

```bash
npm install
```

创建并激活虚拟环境后安装后端依赖：

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r backend/requirements.txt
```

> `backend/requirements.txt` 写的是版本区间，供开发使用；部署走 `backend/requirements.lock`（精确版本）。两者分工见文件头部注释。

在一个终端启动 FastAPI（编辑 `config/fund-compass.json` 即可完成常规配置；未配置模型时基金浏览仍可用，但点击研究会返回配置提示）：

```bash
# Windows
cd backend && ..\.venv\Scripts\python.exe run.py

# macOS / Linux
cd backend && ../.venv/bin/python run.py
```

配置模板见 `config/fund-compass.example.json`。实际的 `config/fund-compass.json` 已加入 `.gitignore`，API Key 不会被提交。只有在需要临时覆盖配置或部署到托管平台时，才使用 `FUND_COMPASS_*` 环境变量；环境变量优先级高于配置文件。

在另一个终端启动前端：

```bash
npm run dev -- --port 4173
```

浏览器访问 `http://127.0.0.1:4173/`，前端会通过 Vite 代理请求 `http://127.0.0.1:8080` 的 FastAPI。API 或 AKShare 不可用时，页面显示错误/空状态，不会展示模拟基金。

Reference 模式默认只读。**公网部署至少应保持 `FUND_COMPASS_PUBLIC_WRITE_ENABLED=false`。**

匿名研究的开关是 `FUND_COMPASS_PUBLIC_RESEARCH_ENABLED`，当前部署里为 `true`（见 `render.yaml`）：账号体系与按用户限流都已经上线，模型调用按登录用户计配额，不再需要靠关掉这个开关来挡住匿名刷模型。注意这个开关由 `create_run` 在**认证之后**检查，配成 `false` 时连登录用户也会被 403。

`PRODUCTION` 模式不会因为修改模式变量就自动获得生产数据。只有在已接入并审核生产数据源后，才设置 `FUND_COMPASS_PRODUCTION_DATA_READY=true`；否则 API 会返回 `PRODUCTION_DATA_NOT_READY`。

AKShare 数据源状态使用 `GET /api/data-sources/status` 获取；基金目录/排行刷新使用 `POST /api/data/funds/refresh`。

## 数据库

**启动时会自动执行 `alembic upgrade head`**（见 `backend/app/db/migrate.py`），手工初始化一般不需要。迁移失败会打印到 stderr，但不会挡住服务启动 —— 基金数据接口仍然可读。

### 部署到临时文件系统的平台时必须换 Postgres

默认库是 SQLite，文件落在 `backend/data/fund-compass.db`。本机或任何有持久磁盘的机器上没问题，但 Render / Railway / Fly 这类平台的容器文件系统是**临时的**：每次部署、以及空闲休眠后唤醒都会重建容器，整个库随之清空。丢的不是行情数据（那些每次都从 AKShare 现拉），而是用户账号、对话、观察列表和风险测评 —— 且没有备份。

> Render 自家的免费 Postgres 只是把这个时间点推到第 30 天（创建满 30 天即删除，14 天宽限），不算解决。用 Neon 或 Supabase 这类不限期免费层。

配一个连接串就行，代码会自行处理异步驱动和 pooler 兼容：

```bash
export FUND_COMPASS_DATABASE_URL="postgresql://user:pass@host/db?sslmode=require"
```

连接串可以**原样粘贴**：`app/db/session.py` 里的 `_normalize_database_url` 会补齐 `+asyncpg` 驱动（裸 `postgresql://` 会让服务启动时崩在找不到 psycopg2）、把 `sslmode` 改写成 asyncpg 认的 `ssl`、丢掉 `channel_binding`，并默认关闭预处理语句缓存 —— 后者是 Neon 的 `-pooler` 端点和 pgbouncer 必需的，否则会撞 `DuplicatePreparedStatementError`。迁移仍是启动时自动执行，空库会被建好表。

仍然可以手动执行：

```bash
cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m app.workers.scheduler
```

> `Base.metadata.create_all` 只建缺失的表，**从不 ALTER 已存在的表**。任何结构变更都必须写进迁移脚本，否则线上老库会静默停留在旧结构。

Worker 会把目录结果写入 raw/profile 快照、任务表和 Outbox；相同 Provider 内容 hash 的重复同步会返回 `UNCHANGED`，不会重复写入同一份快照。

## 验证

```bash
# 后端：从 repo 根或 backend/ 目录跑都可以
.venv\Scripts\python.exe -m pytest backend/tests -q
.venv\Scripts\python.exe -m ruff check backend/

# 前端
npm run typecheck
npm run build
git diff --check
```

CI（`.github/workflows/ci.yml`）在每次 push 到 `main` 和每个 PR 上跑这三项：后端 `pytest` + `ruff check`，前端 `vue-tsc` + `vite build`。

构建产物位于 `dist/`。

## 公网后端

**当前是前后端同源部署**：`backend/app/main.py` 在检测到仓库根存在 `dist/` 时，会把它挂到 `/`（注册在所有 API 路由之后）。一个进程同时提供 API 和界面，浏览器全程只面对一个 origin，不需要 CORS 协商，隧道也只需要暴露一个端口。

前端产物是纯静态文件，理论上也可以单独托管到任意静态平台，但那样后端（FastAPI + AKShare + LLM 调用）必须另行部署，并且需要给前端构建注入 `VITE_API_BASE` 指向后端地址。当前部署没有走这条路。

仓库提供了 `render.yaml`，可在 Render 以 Python 原生进程部署，不需要 Docker。后端的 LLM Key 只配置在后端的 Secret 环境变量中，不暴露给前端。

> 配置 `VITE_API_BASE` 时请填写自己部署的服务地址。`fund-compass-api.onrender.com` 属于另一个项目，不是本仓库后端。
