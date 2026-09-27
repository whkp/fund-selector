# AGENTS.md — 基金罗盘（fund-selector）AI 快速上手

> 本文件面向接手的 AI 代理与开发者：读完这一份即可运行、修改、验证本项目。
>
> **维护约定（必须遵守）**：任何代码、环境、流程变更，尤其是新踩的坑与新约定，
> 要在同一次改动中**同步更新本文件**。避免只改代码、不更新本文件。

---

## 1. 项目是什么

「基金罗盘」：基于公开参考数据的基金研究与筛选全栈应用（非交易系统，不提供收益预测或个性化投资建议）。

- 前端：Vue 3 + TypeScript + Vite（无 Vue Router / Pinia，单工作台 + 登录视图）
- 后端：FastAPI + Python 3.13（uvicorn），单进程同时提供 API 与静态前端
- 数据：AKShare 公开参考数据（目录/排行/历史 NAV）+ 雪球/蛋卷按需补数
- 存储：SQLite（开发默认）/ Neon PostgreSQL（生产，us-east-2，跨洋 ~200ms）
- 公网：`https://funds.kpcode.xyz` → Cloudflare named tunnel → 本机 `127.0.0.1:8080`

**当前维护环境（重要）**：本项目通过 Windows 侧 venv（`.venv/Scripts/python.exe`）运行，但
**代码修改 / 运行 / 测试 / git 操作一律在 WSL Ubuntu-24.04 中执行**（`wsl -d Ubuntu-24.04 -- <命令>`），
项目文件原地共用（WSL 路径 `/mnt/c/Users/hkp/fund-selector`）。调用 Windows venv 的 python.exe
时必须传 **Windows 风格路径**（如 `"C:/Users/hkp/..."`）。

## 2. 目录地图

```text
fund-selector/
├── backend/
│   ├── app/
│   │   ├── main.py             # 全部 API 路由 + lifespan + 中间件（约 1000 行，路由集中在此）
│   │   ├── providers.py        # AKShareProvider（上游适配）+ DataRepository（内存 TTL 缓存、
│   │   │                       #   目录/NAV 落库调度、数据库回收、全集评分重算）
│   │   ├── scoring.py          # 多维评分引擎（score-v1）：五维度归一化、全集百分位、
│   │   │                       #   透明降级、build_reason() 一句话结论
│   │   ├── enrichment.py       # 按需/批量补数：雪球 basic_info/achievement + 蛋卷 risk_level，
│   │   │                       #   持仓快照、补数后就地重算该只评分
│   │   ├── llm.py              # OpenAI-compatible / Ollama 客户端 + 输出校验
│   │   ├── agent_service.py    # agent 模式研究编排（tau harness 工具循环）
│   │   ├── agent_tools.py      # agent 工具集（search_funds / get_fund_detail / 净值历史等）
│   │   ├── auth.py             # 注册/登录/JWT/对话/邀请码策略
│   │   ├── ratelimit.py        # 限流（登录/注册/研究配额）
│   │   ├── security.py         # 口令策略、PBKDF2、JWT 签发校验
│   │   ├── config.py           # config_value()：env > config/fund-compass.json > 默认值
│   │   ├── models.py           # Fund / SourceStatus 等领域模型
│   │   ├── metrics.py          # metric-v1：收益/年化/波动率/最大回撤/夏普
│   │   ├── knowledge.py        # 本地 Markdown 知识库适配器（可换 RAG）
│   │   ├── watchlist.py        # 观察列表（已持久化到数据库）
│   │   ├── db/
│   │   │   ├── base.py         # 全部 ORM 模型（16 张表，含持仓快照）
│   │   │   ├── session.py      # 引擎/会话工厂 + 连接串规范化（asyncpg/sslmode/pooler）
│   │   │   ├── migrate.py      # ensure_schema()：启动时跑 alembic upgrade head
│   │   │   ├── snapshot_repository.py  # 目录快照读写（gzip payload）
│   │   │   └── nav_repository.py       # 历史 NAV 持久化 + load_latest_history 回收
│   │   └── workers/            # fund_universe / nav_history / enrichment / outbox / scheduler（周期调度）
│   ├── migrations/             # Alembic；env.py 含日志关键修复（见第 8 节）
│   ├── tests/                  # 后端单元/API/worker 测试（不触网，见第 5 节）
│   ├── tau_agent/ tau_ai/      # vendored 自 huggingface/tau（MIT，不参与本仓库 lint）
│   ├── requirements.txt        # 开发用版本区间；部署用 requirements.lock（精确版本）
│   └── run.py                  # 后端入口：UTF-8 流重配 + dotenv + uvicorn
├── src/                        # 前端（App.vue 主工作台、views/LoginView.vue、services/）
├── config/fund-compass.json    # 运行时配置（gitignored；example 为模板）
├── scripts/                    # 运维脚本（见第 4 节）
├── docs/                       # 设计文档与进展记录（部分内容较旧，以代码为准）
├── logs/                       # launcher.log / backend.log / tunnel.log
└── dist/                       # 前端构建产物；main.py 检测到后挂载到 /
```

## 3. 技术栈要点

- 后端依赖（`backend/pyproject.toml`）：fastapi、uvicorn、pydantic-settings、httpx、pandas、numpy、akshare、SQLAlchemy 2、asyncpg、aiosqlite、alembic、greenlet
- Ruff 规则集显式固定（E/F/W/I/UP/B/SIM/BLE/LOG/RET），**不要**修改规则集默认值依赖；中文注释不受行宽卡点
- Python 版本以 `.python-version` / Dockerfile / CI 三处保持一致（3.13）
- 前端仅依赖 vue / vite / vue-tsc / lucide 图标

## 4. 快速开始与运行

### 4.1 本地开发

```bash
# 后端（WSL 内；注意 python.exe 用 Windows 路径传参）
cd /mnt/c/Users/hkp/fund-selector/backend && ../.venv/Scripts/python.exe run.py

# 前端（另开终端）
cd /mnt/c/Users/hkp/fund-selector && npm run dev -- --port 4173
```

- 后端监听 `127.0.0.1:8080`（`FUND_COMPASS_API_ADDR` / config `server.address`）
- 前端 4173 通过 Vite 代理把 `/api` 转发到 8080
- **启动前必须查净 8080**：`netstat -ano | findstr ":8080"`（Windows 侧），有 listener 先杀再启
- 配置优先级：`FUND_COMPASS_*` 环境变量 > `config/fund-compass.json` > 默认值；
  `.env` 位于仓库根（run.py 显式加载）

### 4.2 公网服务（本机即“生产”）

```text
scripts\start-public.cmd   # 幂等：后端已在跑则跳过，cloudflared 已在跑则跳过
scripts\run-backend.cmd    # 后端 + 日志重定向 logs\backend.log
scripts\run-tunnel.cmd     # named tunnel（funds.kpcode.xyz -> 127.0.0.1:8080）
scripts\show-invite-code.cmd / show-public-url.cmd
```

- 后端重启后会经历 1~2 分钟装载（见 6.1），期间 `/health` 可能已 200 但目录未就绪
- 隧道凭据在 `%USERPROFILE%\.cloudflared\`（cert.pem / <tunnel-id>.json / config.yml）
- 分离重定向模式下日志在 `logs/backend_stdout.log` 与 `logs/backend_stderr.log`
  （uvicorn INFO 走 stderr；应用日志同 stderr）

### 4.3 重启后端的正确姿势

1. `netstat -ano | findstr ":8080"` 找到真进程 PID（可能还有一个 shim 父进程）
2. 杀掉（`Stop-Process -Id <pid> -Force`，父 shim 一并杀）
3. `Start-Process` 启动（参考 `shots/_tmp/restart_backend_*.ps1` 的分离重定向写法）
4. 等待并确认 stderr 日志出现「已从数据库装载基金目录 N 只」+「已从快照回填 N 只」

## 5. 测试与静态检查（不触网、不污染库）

```bash
# 后端全量（在 backend/ 下）
cd /mnt/c/Users/hkp/fund-selector/backend
../.venv/Scripts/python.exe -m pytest -q --disable-warnings
../.venv/Scripts/python.exe -m ruff check app tests migrations
```

- `tests/conftest.py` 在导入任何 app 模块前**强制**指向临时 SQLite、关闭 AKShare、
  固定邀请码、清掉全部 LLM 变量并把配置文件指向不存在路径 —— **测试绝不允许触网/真调模型**
- 涉及数据库的测试有两种模式：全局测试库（conftest 的临时库）+ 私有库
  （`tmp_path` + monkeypatch URL + `Base.metadata.create_all`）
- 后台落库任务是 `asyncio.create_task`，测试结束前必须 drain
  （`_drain_persist` / `_drain_history_persists`），否则出现 pending task 警告
- 回收类测试先清表（`_clear_history_snapshots`），保证「未命中」分支确定成立
- 前端：`npm run typecheck` + `npm run build`
- CI（`.github/workflows/ci.yml`）：push/PR 跑后端 pytest + ruff，前端 typecheck + build；本地阶段三新增迁移、持仓快照和 worker 测试需一并覆盖

## 6. 架构与关键数据流

### 6.1 启动时序（backend/app/main.py 的 lifespan）

1. `ensure_schema()`：alembic upgrade head（**失败不挡启动**，错误打到 stderr）
2. 打印 `[startup]` 邀请码策略（只打掩码，完整码走 `scripts/show-invite-code.cmd`）
3. `asyncio.create_task(_warm_up())` —— **后台跑，不阻塞 startup**：
   a. `load_fund_universe()` 从 Neon 读 gzip 快照（约 943KB，跨洋 60~70s）
   b. `adopt_universe()` 原子替换内存目录并重建 n-gram 索引
   c. `ensure_funds()`：TTL（6h）内不打上游，过期刷新，失败回退旧快照
   d. `load_enriched()` 回填补数主数据（fund_profile/metric 快照）

> 现象提示：`Application startup complete.` 先出现，装载日志要等 1~2 分钟。
> 这不是卡死（历史上曾被误判），正确性由「已从数据库装载…」日志确认。

### 6.2 基金目录（全量 ~20390 只）

- 上游：`ak.fund_name_em()` + `ak.fund_open_fund_rank_em(symbol="全部")`，
  解析在 `to_thread` 里跑（2 万行解析不占事件循环）
- 内存：`DataRepository.funds`（dict）+ n-gram 索引（`build_gram_index`，也在线程里）
- 落库：刷新成功后 `_schedule_universe_persist` 后台任务写
  `funds` + `fund_profile_snapshots` + `raw_data_snapshots`（endpoint=`fund-universe`，**只存 gzip**）+ `job_runs` + `outbox_events`
- 去重：同 content_hash 返回 `UNCHANGED`；旧明文快照同内容再落库时就地升级为 gzip（`COMPRESSED`）
- 手工刷新接口：`POST /api/data/funds/refresh`；补数命令：`python -m app.workers.enrichment --limit N`；`python -m app.workers.scheduler` 启动每日调度。

### 6.3 历史 NAV（按需落库 + 数据库回收）

`/api/funds/{code}/history`（默认 `period=1年`）→ `DataRepository.history()`：

1. 内存 TTL 缓存（`histories` dict，6h）命中直接返回
2. 未命中且 `period=="1年"`：`_recover_history()` 查数据库快照
   （`load_latest_history`，gzip 优先，`business_date` 距今 ≤4 天才回收）→ 回填缓存返回
3. 仍未命中：打 AKShare；成功后 `_schedule_history_persist` 异步落库
   （只落目录内基金；同基金在途任务不重复；防堆积）
4. 落库内容：`raw_data_snapshots`（endpoint=`fund-history:{code}`，gzip）+ `fund_nav_snapshots`

> 只对 `1年` 窗口落库/回收；`1月/3月/6月/3年` 每次回源。
> `workers/nav_history.py` 是批量预填充骨架，暂无调用点（按需路径已实现，勿误删）。

### 6.4 补数（enrichment）

- 触发：agent 的 `get_fund_detail` 命中占位字段时逐只补，**每轮研究预算 ≤6 只**
- 来源：雪球 `fund_individual_basic_info_xq` / `fund_individual_achievement_xq`（回撤）+
  蛋卷 `danjuanfunds.com/djapi/fund/{code}`（risk_level 1~5 → 中文标签）
- 效果：内存 Fund 直接改 + 重建索引（新经理/主题立即可检索）→ 落
  `fund_profile_snapshots` / `fund_metric_snapshots` → 启动 `load_enriched()` 回填
- 批量 worker 默认处理浏览前 200、各主题头部 50 和观察列表，最多 3 并发、指数退避、
  `needs_enrichment()` 跳过内存中已齐字段；JobRun 进度写入串行，部分失败标为 PARTIAL；持仓写入 `fund_holding_snapshots` 的 gzip payload。
- `GET /api/funds/{code}/holdings` 提供详情穿透，`GET /api/watchlist/overlap` 在共同重仓达到 5 只时告警。
- `GET /api/data/coverage` 返回目录、核心字段、持仓和 NAV 缓存覆盖度。

### 6.5 LLM 研究链路

- `POST /api/recommendations/runs`：硬约束筛选（候选池 ≤60）→ 知识检索（≤5 片段）→
  单次 Chat Completions（或 agent 模式：`agent_service` 走 tau harness 多轮工具循环）→
  Pydantic Schema + 候选代码白名单 + 去重校验，失败明确报错，**不回退规则推荐**
- 支持 BYOK（页面内存，不落库）；服务端 Key 只放 env/config，绝不下发前端
- 研究配额在调用模型**之前**原子占位（防并发刷爆）；`FUND_COMPASS_PUBLIC_RESEARCH_ENABLED`
  在**认证之后**检查 —— 配 false 时登录用户也会 403，默认 true（配额已挡匿名）

### 6.6 认证与限流

- 邀请码：`FUND_COMPASS_INVITE_CODE`（多码逗号分隔）；缺省时自动生成并存
  `backend/data/.invite-code`。注册前校验，掩码展示
- JWT：密钥来自 env 或文件；**部署平台必须固定密钥**（文件系统临时的话每次部署踢人下线）
- 限流：登录按 IP + 账号、注册按 IP + 全局、研究按用户小时/天 + 全局天

## 7. 数据库与迁移

- 开发默认 SQLite `backend/data/fund-compass.db`；生产 Neon（连接串**原样粘贴**即可）
- `db/session.py` 自动规范化：补 `+asyncpg`、`sslmode`→`ssl`、去 `channel_binding`、
  默认关闭 prepared statement cache（Neon `-pooler`/pgbouncer 必需）
- 16 张表：`funds`、`raw_data_snapshots`、`fund_profile_snapshots`、`fund_nav_snapshots`、
  `fund_quote_snapshots`、`fund_metric_snapshots`、`job_runs`、`outbox_events`、`users`、
  `conversations`、`conversation_messages`、`risk_profiles`、`watchlist_items`、
  `recommendation_runs`、`recommendation_steps`、`fund_holding_snapshots`
- **结构变更必须写 alembic 迁移**：`Base.metadata.create_all` 只建缺失的表、从不 ALTER
- 迁移 head（截至 2026-09-27）：`20260927_0004`（持仓 gzip 快照）
- `recommendation_runs` / `recommendation_steps` 表已建但**暂无代码使用**
- `outbox_events` 由 scheduler 每 5 分钟消费并写审计日志；暂不发送外部通知

## 8. 踩坑与硬约定（接手必读）

按「症状 → 根因 → 正确做法」整理，全部为真实踩过的坑：

1. **服务日志静默但服务正常**（startup complete 消失、应用日志全灭）
   根因：`migrations/env.py` 的 `fileConfig()` 默认 `disable_existing_loggers=True`，
   启动路径跑 ensure_schema 时禁掉所有已存在 logger。
   现状：已修为 `fileConfig(config.config_file_name, disable_existing_loggers=False)` —— **勿回退**。
2. **日志中文乱码**（`已从数据库…` 变乱码）
   根因：Windows 重定向到文件用 GBK 编码。
   现状：`run.py` 启动时把 stdout/stderr `reconfigure(encoding="utf-8")` —— 勿删。
3. **跨洋库 ORM 全实体读大字段导致卡死**（worker 卡 8 分钟、启动装载不可用）
   根因：`select(Entity)` 会把 MB 级 payload 整行拉回，跨洋低带宽放大成分钟级。
   做法：**列裁剪**（`select(Entity.id)` 做存在性判断）；更新用 SQL UPDATE + `synchronize_session=False`；
   大字段只存 gzip（`payload_gz`），读路径先取定位列再按需取 payload 列。
   本项目已三次踩中（worker、nav_repository、snapshot 装载），改任何查库代码都要过一遍这条。
4. **公网 API 脚本被 403**，响应仅 `error code: 1010`
   根因：Cloudflare Browser Integrity Check 拦 `Python-urllib` 等默认 UA。
   做法：验证脚本/curl 一律带浏览器 UA（`curl -A "Mozilla/5.0 ..."`）。
5. **PowerShell 调 WSL 的转义地狱**（`|`、`$`、`%`、`\[` 被破坏）
   做法：不写内联复杂命令，一律先写 `.ps1` / `.sh` 脚本文件再执行；
   WSL 里 grep 大日志要加 `-a`（避免 binary file matches 吞输出）。
6. **8080 端口冲突**：启动后端前先 `netstat` 查净，宁可多杀一个 shim 父进程。
7. **新增 `/api/*` 路由必须挂 `Depends(current_user)`**（有登录体系后的一致性要求）。
8. **测试绝不触网/绝不用真实密钥**：conftest 已强制；新增测试沿用同样的隔离方式。
9. **长任务不许占事件循环**：抓取/解析/索引构建走 `asyncio.to_thread`；
   跨洋写库走后台 `create_task` + done callback 记日志 + 防堆积。
10. **git 提交**：仓库无本地 git 身份配置，提交用
    `git -c user.name=kp -c user.email=kp@kpdeMacBook-Air.local commit ...`（勿改 git config）；
    **本地 commit 可以，push 必须等用户明确指令**。远端 origin：github.com/whkp/fund-selector。
11. **产品边界**：AKShare/雪球/蛋卷均标记 LOW 信任公开参考；不能当授权生产数据；
    普通基金 NAV 是日频披露，不是盘中实时。
12. **注释与文档用中文**，注释要解释「为什么」而不是复述代码（项目一贯风格）。
13. **评分计算必须在线程池里跑**：`_recompute_scores(funds)` 对全集 2 万只基金做归一化，
    若在事件循环里执行会阻塞所有请求。调用方式：`await asyncio.to_thread(_recompute_scores, funds)`。
14. **新增评分维度必须同步更新 `scoring.py`、`types.ts` 的 `ScorePart` 类型、
    以及 `test_scoring.py` 的边界用例**；三者不同步会导致前端渲染异常或测试漏洞。
15. **`scoring.py` 是纯函数模块**：无 IO、无 asyncio、无数据库调用，可安全在线程池或同步上下文调用；
    不要在里面引入任何异步依赖。

## 9. 常见任务手册

| 任务 | 步骤 |
|---|---|
| 改后端逻辑 | 改代码 → WSL 跑 pytest + ruff → 重启后端（查净 8080）→ 看日志确认 |
| 加 API 路由 | `main.py` 加路由（挂 `current_user`）→ `tests/test_api.py` 加用例 → 重启验证 |
| 改数据库结构 | 新增 `migrations/versions/*.py` 迁移 → 本地跑 ensure_schema → 测试覆盖 |
| 改前端 | `npm run typecheck` → `npm run build` → 同源部署下刷新即生效（dist 重新生成） |
| 验证公网 | 用带浏览器 UA 的脚本/curl 打 `https://funds.kpcode.xyz/health` 与登录+业务接口 |
| 清理测试账号 | Neon 直连 asyncpg（参考 `shots/_tmp/cleanup_*`），按邮箱白名单精确删除 |
| 发布 | 本地 commit（-c 临时身份）→ 等用户明确「推送」再 push |

## 10. 已知限制与待办（截至 2026-09-27）

- 阶段二/三自动验证已通过：170 项 pytest（2 条第三方弃用警告）、ruff、typecheck、生产 build；包含临时库 ensure_schema、旧库 0003→0004 升降级、worker 重试/跳过/失败进度、调度器暂停启动。浏览器交互、真实上游和生产部署验收仍待完成。
- `workers/scheduler.py` 已接 APScheduler，每日 02:00 刷新目录、02:20 批量补数、03:00 预填充头部 50 只 NAV；每 5 分钟消费 outbox。APScheduler 3.11.0 / tzlocal 5.3.1 已安装，requirements 和 pyproject 已同步
- NAV 仅 `1年` 窗口落库/回收；1月/3月/6月从 1年切片，3年仍需回源
- scheduler 已经通过 Repository.history 接入热门 NAV 预填充；独立 workers/nav_history.py 保留单只同步入口
- `recommendation_runs` / `recommendation_steps` 表未启用；研究记录目前仅存于对话
- 持仓只覆盖精选集合，数据源为 AKShare 公开披露，季度披露之间可能过期
- 前端无路由/Pinia；`dist/` 需手动 `npm run build` 后同步
- `docs/开发进展.md` 停留在 2026-09-10，晚于它的进展以本文件与 README 为准
- 阶段三首版新增 `fund_holding_snapshots`（迁移 `20260927_0004`）、批量补数 worker、持仓重叠 API 和覆盖度卡片；持仓来自 AKShare 定期公开披露，不能视为实时持仓
- 环境阻碍已定位：沙箱隐藏 WSL 互操作套接字时会报 `UtilBindVsockAnyPort`；经工具提权批准后，从 WSL 调用现有 Windows Python 3.13.14 可正常测试，无需重建 venv。不要用 shim 替换依赖或绕开 conftest 来宣称全量测试通过。
- 当前 node_modules 为 Windows 安装，Linux Vite 缺少原生 Rollup/esbuild 包时，可从 WSL 调用 `node.exe node_modules/vue-tsc/bin/vue-tsc.js -b`，再调用 `node.exe node_modules/vite/bin/vite.js build`（对应 npm run build）；本次已构建 dist。安装依赖的网络错误应按工具规定提权重试。
- 数据源状态在「仅从 DB 装载、未打上游」时显示 `PENDING`（前端可能提示"等待首个快照"，语义正确但略易困惑）
- 评分（score-v1）的规模/经理年限维度在 AKShare 目录不提供这两个字段时必须经过雪球补数才能拿到非 null 值；冷启动阶段的基金这两维度会显示「未获取」，补数后自动更新
- 场景预设已提供 `GET /api/funds/screen?preset=...` 与前端按钮，未知 preset 返回 400

## 11. 文档索引

- `README.md`：运行、数据库、验证、公网部署（较新，可信）
- `docs/基金罗盘-项目设计文档.md`：总体设计（677 行）
- `docs/开源项目调研与借鉴.md`：qlib/OpenBB/TradingAgents/xalpha/investool 等调研与可借鉴设计（2026-09-27）
- `docs/开发进展.md`：历史进展（较旧）
- `docs/数据更新与来源.md`：数据来源边界
- `docs/LLM-接入方案.md`：模型接入
