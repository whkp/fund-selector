# TODO — 基金罗盘新手决策优化

> 来源：计划「基金罗盘新手决策优化」（2026-09-27 批准）。本文件是它在仓库内的可勾选落地清单。
> 用法：完成一项就在 `[ ]` 打勾成 `[x]`；每阶段收尾跑全量 `pytest` + `ruff` + 前端 `typecheck`/`build`，
> 并同步更新 `AGENTS.md`。
> 约定：代码/测试/git 在 WSL Ubuntu-24.04 执行（远程 push 走 Windows 侧）；测试不触网、不用真实密钥。

**当前状态**：阶段一完成（1.1～1.3 全部落地，1.4 测试已更新）。

---

## 目标与总原则

让新手打开页面就能「看得懂分数、知道选哪只、敢下手」。差异化路线：**补齐关键数据**（批量补 risk/经理/规模/回撤 + 持仓穿透），喂给**透明可解释的多维评分**——拼「每只都讲得清、按风险画像适配」，不拼数据条数。

---

## 阶段一：性能止血（消除「加载慢」）

- [x] **1.1 ensure_funds 改 serve-stale-while-revalidate**
  - 文件：`backend/app/providers.py` 的 `ensure_funds()`、`_funds_are_fresh()`
  - 有旧数据但过期时立即返回旧数据 + 后台单飞刷新（`_schedule_funds_refresh` / `_refresh_funds_guarded`，keep-reference + in-flight 跳过）；仅内存全空时阻塞等待。
- [x] **1.2 history 短窗口从「1年」序列切片派生**
  - 文件：`backend/app/providers.py` 的 `history()`，新增 `_SHORT_WINDOWS = {1月,3月,6月}`
  - 先拿 1年（缓存/DB 回收/上游），再用 `AKShareProvider._limit_history_period` 按天数切片；3年/5年 仍回源。
- [ ] **1.3 冷启动装载期请求降级 + 前端骨架**（部分完成）
  - [x] 后端：`DataRepository` 加 `loading` 标志 + `_loaded_event` + `mark_loaded()` + `is_ready()`；`ensure_funds()` 装载中等待；`main.py._warm_up` 置 loading / finally `mark_loaded()`。
  - [ ] `/api/funds` 未就绪时返回 `LOADING` 质量状态（复用 `envelope()`）。
  - [ ] 前端 `src/App.vue` 的 `bootstrap()` 识别 LOADING，显示「数据装载中」骨架。
- [ ] **1.4 阶段一验证**
  - [ ] `backend/tests/test_repository_ttl.py`：stale-while-revalidate（过期立即返回旧数据 + 后台刷新一次）、短窗口切片派生（1月/3月 不打上游、结果等于 1年 尾部切片）、装载中降级。
  - [ ] 全量 `pytest` + `ruff` 绿；本地重启后端实测详情打开延迟。

---

## 阶段二：透明多维评分 + 新手决策引导（解决「只有一个得分、不会选」）

- [ ] **2.1 新增 `backend/app/scoring.py`，评分改为动态计算**
  - `compute_score(fund) -> tuple[int, list[dict]]`；维度（归一化 0~100 后加权，权重可在 config 调）：
    收益（近1年+近3年年化，风险调整后）、风险（最大回撤/波动率，优先用已落库 1年 NAV 自算，补数后择优）、
    稳定性（夏普/卡玛、多周期一致性）、成本与规模（费率低、规模适中）、经理/主题（任职年限、主题清晰度）。
  - 输出 `score_parts`：每维 `{label,value,weight,color}`，**真正填充**（当前恒空）。
  - 缺字段维度标「未获取」、不计入加权分母（透明降级）。版本化 `score-v1` / `metric-v2`。
- [ ] **2.2 评分计算时机（保持请求路径快）**
  - 在 `adopt_universe()`/`refresh_funds()`/`load_enriched()`/`enrich_fund()` 完成后重算 score+score_parts 存回 Fund；`as_dict()` 只读取。
  - 移除 `providers.py` L225 烤死分数的启发式，改调 `scoring.compute_score`；`main.py` 的 `query_score()` 基线分改用新评分。
- [ ] **2.3 新手决策引导（后端产出 + 前端呈现）**
  - 后端：`reason` 升级为「一句话结论 + 关键证据数字」；`highlights`/`caveat` 给标签与风险；新增「适合谁」映射（保守/稳健/进取，复用 `risk_profiles`）。
  - 前端 `src/App.vue`：卡片展示分解评分条 + 维度徽章 +「适合谁」；详情抽屉展示「为什么是这个分」；`compass-card` 四维条接真实 `scoreParts` + 新手解读文案。
  - 场景化预设（借鉴 investool 4433）：`screen_funds` / `/api/funds/screen` 增 `preset` 参数（`4433`/`低回撤稳健`/`主题`），前端一键触发。
- [ ] **2.4 阶段二验证**
  - `backend/tests/test_scoring.py`（各维度归一化、缺字段降级不加权、score_parts 完整、4433 预设正确）。
  - `test_api.py`：卡片返回非空 scoreParts、preset 生效；前端 `typecheck` + `build` + 截图核对。

---

## 阶段三：批量补齐关键数据 + 持仓穿透（差异化）

- [ ] **3.1 批量补数 worker**
  - 新文件 `backend/app/workers/enrichment.py`（复用 `enrichment.enrich_fund`）：对精选集合（浏览前 N / 关注列表 / 各主题头部）批量补 risk/经理/公司/规模/回撤/波动/主题/成立年限，落 Neon。
  - 并发限流 + 指数退避重试 + 断点续跑（`needs_enrichment` 为假跳过）；进度写 `job_runs`；命令 `python -m app.workers.enrichment`。
- [ ] **3.2 持仓穿透 / 伪分散检测（借鉴 xalpha）**
  - `enrichment.py` 增 `_fetch_holdings`（雪球/天天基金前十大重仓）。
  - 新表 `fund_holding_snapshots`（alembic 迁移，只存必要列 + gzip，避免跨洋读大字段坑）。
  - 补数 worker 写持仓；`watchlist` 页重叠预警；详情抽屉持仓穿透。
- [ ] **3.3 周期调度 + 事件消费者**
  - `backend/app/workers/scheduler.py` 接 APScheduler/系统计划任务：每日增量刷新目录 + 批量补数 + 预填充热门 history（复用 `workers/nav_history.py`）。
  - `outbox_events` 加后台消费者（先做审计落库/日志）。
- [ ] **3.4 数据覆盖度可视化**
  - 前端展示「已补齐 N 只关键字段 / 持仓穿透覆盖 N 只」进度条 + 差异化文案。
- [ ] **3.5 阶段三验证**
  - `backend/tests/test_enrichment_worker.py`（私有 SQLite + FakeProvider，不触网）：幂等、断点续跑、持仓落库与重叠检测；迁移本地 `ensure_schema` 跑通；全量 pytest + ruff 绿。

---

## 交付与验证约定（贯穿三阶段）

- 每阶段结束：全量 `pytest` + `ruff` + 前端 `typecheck`/`build`；本地重启后端实测关键路径延迟与评分展示。
- 每阶段同步更新 `AGENTS.md`：新增文件/表/评分口径/策略变化与新踩的坑；更新第 10 节「已知限制」（移除已解决项）。
- 结构变更一律写 alembic 迁移（`create_all` 从不 ALTER）；查库遵循列裁剪、大字段只存 gzip。
- 远程 push 走 Windows 侧 git；本地 commit 用临时身份，push 等明确指令。

## 假设

- 短窗口（1月/3月/6月/今年来）可由 1年 日频 NAV 序列切片得到，3年/5年 需单独回源。
- 评分维度权重给合理默认可在 config 调；不引入 LLM 打分（保持确定性、可解释、零额外延迟）。
- 批量补数与持仓沿用雪球/蛋卷公开参考口径（LOW 信任），仅用于研究展示。
- 持仓穿透先覆盖精选集合而非全量 2 万只。
