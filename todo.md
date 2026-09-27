# TODO — 基金罗盘新手决策优化

> 来源：计划「基金罗盘新手决策优化」（2026-09-27 批准）。本文件是它在仓库内的可勾选落地清单。
> 用法：完成一项就在 `[ ]` 打勾成 `[x]`；每阶段收尾跑全量 `pytest` + `ruff` + 前端 `typecheck`/`build`，
> 并同步更新 `AGENTS.md`。
> 约定：代码/测试/git 在 WSL Ubuntu-24.04 执行（远程 push 走 Windows 侧）；测试不触网、不用真实密钥。

**当前状态**：阶段二/三首版已实现，自动检查已通过（170 项 pytest、ruff、typecheck、生产 build）；浏览器交互和真实上游任务验收仍待完成。

**验证记录（2026-09-27）**：通过 WSL 调用现有 Windows Python 3.13.14，170 项 pytest 通过；使用 conftest 临时 SQLite，禁用真实上游和模型。包含 ensure_schema、空库迁移升降级、旧库 0003→0004 升降级、worker 重试/跳过/失败状态、调度器暂停启动。ruff、前端 typecheck、生产 build 和 diff 检查通过。2 条警告来自 Starlette/httpx、AnyIO 的弃用提示。未将临时 shim/移出 conftest 的探索测试计入正式结果。

---

## 目标与总原则

让新手打开页面就能「看得懂分数、知道选哪只、敢下手」。差异化路线：**补齐关键数据**（批量补 risk/经理/规模/回撤 + 持仓穿透），喂给**透明可解释的多维评分**——拼「每只都讲得清、按风险画像适配」，不拼数据条数。

---

## 阶段一：性能止血（消除「加载慢」）✅

- [x] **1.1 ensure_funds 改 serve-stale-while-revalidate**
  - `providers.py`：TTL 过期但有旧数据时立即返回旧快照 + 后台单飞刷新
    （`_schedule_funds_refresh` / `_refresh_funds_guarded`，in-flight 跳过）；仅内存全空时阻塞。
- [x] **1.2 history 短窗口从「1年」序列切片派生**
  - `providers.py`：新增 `_SHORT_WINDOWS = {1月,3月,6月}`；先拿 1年（缓存/DB/上游），
    再用 `AKShareProvider._limit_history_period` 按天数切片，省掉每个短窗口单独回源的跨洋开销。
- [x] **1.3 冷启动装载期请求降级 + 前端骨架**
  - 后端：`loading` 标志 + `_loaded_event` + `mark_loaded()` + `is_ready()`；
    `/api/funds` 未就绪时返回 `qualityStatus: "LOADING"` + `message`。
  - 前端：`fetchFunds` 返回 `FundsResult` 联合类型；`bootstrap()` 识别 LOADING 后
    启动 8s 轮询直到就绪；期间显示 6 张 shimmer 骨架卡片 + 装载横幅。
- [x] **1.4 阶段一验证**
  - `test_repository_ttl.py` 更新为 stale-while-revalidate 语义，新增 `_drain_refresh`；
    全量 142 tests 通过，ruff 干净，typecheck 干净。

---

## 前端动效升级 ✅（2026-09-27 完成）

- [x] 卡片 hover 微光扫过（`::after` transform）
- [x] 研究按钮脉冲光晕（`run-pulse` keyframe）
- [x] 详情抽屉评分数字渐变色 + `score-pop` 弹出动画
- [x] 评分数字动态计数器（JS `animateScore`，0 跳到目标值）
- [x] 抽屉各区块错落入场（header/score/chart/metric 各 delay）
- [x] 研究步骤从左侧滑入（`step-slide` 替换原 `step-in`）
- [x] 页面 section 切换淡入上移（`section-enter`）
- [x] Toast 弹出 scale + translateY（`toast-pop`）
- [x] 侧边栏品牌圆环呼吸光晕（`brand-breathe`）
- [x] 罗盘核心呼吸 + 微缩放（`core-breathe` 增强）
- [x] 骨架卡片 shimmer 错落延迟
- [x] 评分进度条各维度错落生长延迟
- [x] 收益数字红涨绿跌着色（`positive` / `negative` class）

---

## 阶段二：透明多维评分 + 新手决策引导 ✅

### ✅ 2.1 新建 `backend/app/scoring.py` — 多维评分引擎（已完成）

`compute_score(fund, universe?)` 五维度：近1年收益(30%)、风险控制(25%)、成本效率(20%)、规模健康(15%)、经理经验(10%)。全集百分位归一化；无全集时绝对值兜底；缺字段 null 透明降级不计入分母。`score_parts` 每项含 `label/value/weight/color/raw/basis`，版本 `score-v1`。

---

### ✅ 2.2 评分集成到数据管道（已完成）

`providers.py` `refresh_funds()` / `adopt_universe()` 加全集重算（`asyncio.to_thread(_recompute_scores)`）；`enrichment.py` 单只补数后就地重算；移除旧启发式评分。

---

### ✅ 2.3 前端接真实 scoreParts（已完成）

`types.ts` 升级 `ScorePart` 类型（`value: number | null`，新增 `raw` / `basis`）；卡片加 3 条迷你进度条（tooltip 含 basis）；详情抽屉展示全部 5 条维度分解，错落入场；`.card-score-bars` / `.drawer-score-parts` 新样式。

---

### ✅ 2.4 reason 字段升级（已完成）

`build_reason()` 取最强 2~3 条有效维度拼一句话结论，兜底文案「数据待补充」。`adopt_universe` 和 `enrich_fund` 后自动更新。

---

### 2.5 场景预设按钮（一键筛选）

> **目标**：新手不用填写任何条件，点一下「4433 策略」或「低回撤稳健」就能看到合适的候选。

**后端**（`main.py`，`/api/funds/screen` 或在现有 `screen_funds` 增加 `preset` 参数）：
```python
PRESETS = {
    "4433": ScreenRequest(filters=FilterCriteria(maxFee=1.0, riskLevelMax="中高风险"), query="4433"),
    "低回撤稳健": ScreenRequest(filters=FilterCriteria(maxFee=0.8, riskLevelMax="中风险"), query="回撤小 稳健"),
    "科技主题": ScreenRequest(filters=FilterCriteria(fundTypes=["股票型"]), query="科技 半导体 AI"),
}
```
- `GET /api/funds/screen?preset=4433` 返回与现有 `/api/funds?q=...` 相同的 envelope 结构。
- preset 不存在时返回 400，不静默降级。
- 路由挂 `Depends(current_user)`。

**前端**（`src/App.vue`，筛选面板顶部）：
- 三个预设按钮：「📈 4433 策略」「🛡️ 低回撤稳健」「💡 科技主题」。
- 新增 `activePreset = ref('')` 和 `fetchPreset(name)` 函数；点击后调用 `/api/funds/screen?preset=xxx`，结果覆盖 `availableFunds`，高亮激活的预设按钮。
- 清除预设按钮（或搜索时）重置 `activePreset`，回到浏览视图。
- `src/services/fund-api.ts` 新增 `fetchPreset(name)` 函数。

---

### 2.6 阶段二验证（自动检查通过，交互验收待完成）

- [x] `backend/tests/test_scoring.py`（已有 18 个用例）新增：
  - `build_reason` 含数字的 edge case（`raw="--"` 的基金）
- [x] `test_api.py` 新增：
  - `/api/funds` 返回的基金 `scoreParts` 非空（至少 1 条有效维度）。
  - `/api/funds/screen?preset=4433` 返回 200 且 items 非空。
  - `/api/funds/screen?preset=不存在` 返回 400。
- [x] 前端 `typecheck` 与生产 build 通过。
- [ ] 浏览器手动验证：
  - 卡片显示真实维度条（非全灰「未获取」短条）。
  - 抽屉进度条有颜色有数字，`null` 维度显示「未获取」。
  - 预设按钮点击后候选列表更新，激活态高亮。
- [x] 全量 `pytest`（170 passed）+ `ruff` 通过，已更新 `AGENTS.md`。

> **目标**：把卡片上的四条假进度条换成有意义的真实数字，每维度都有数据来源说明。

**评分函数签名**
```python
def compute_score(fund: Fund) -> tuple[int, list[dict[str, Any]]]:
    """返回 (总分 0-100, score_parts 列表)。"""
```

`score_parts` 每项结构：
```python
{"label": "近1年收益", "value": 72, "weight": 0.30, "color": "#1a3358",
 "raw": "+18.4%", "basis": "AKShare 排行字段"}
```

**五个维度**（默认权重可在 `config/fund-compass.json` 的 `scoring.weights` 调整）：

| 维度 | 权重默认 | 数据字段 | 归一化方式 |
|---|---|---|---|
| 近1年收益 | 0.30 | `fund.one_year` | 全集百分位，-30% → 0，+50% → 100 |
| 风险控制 | 0.25 | `fund.drawdown`（优先），`fund.volatility` | 回撤越小越高，-5% → 100，-50% → 0 |
| 成本效率 | 0.20 | `fund.fee` | 费率越低越高，0.1% → 100，1.5% → 0 |
| 规模健康 | 0.15 | `fund.scale`（亿） | 2亿~50亿区间满分，过小/过大折减 |
| 经理经验 | 0.10 | `fund.manager_years` | 任职 ≥5年满分，线性折减到 0 |

**缺字段处理**：
- 字段为 `None` 或「未获取」时，该维度标 `"basis": "未获取"`, `"value": null`，
  不计入加权分母（透明降级，不影响其他维度的权重比例）。
- 总分 = `sum(part.value * part.weight for part in parts if part.value is not None)`
  除以有效权重之和，再乘 100 取整。

**版本标记**：所有计算结果附 `score_version: "score-v1"`，迁移时可回溯。

**实现步骤**：
1. 新建 `backend/app/scoring.py`，纯函数，无 IO，不依赖 asyncio。
2. 需要全集百分位的维度（收益、回撤）：`compute_score` 接受可选的 `universe: list[Fund]`
   做百分位计算；若不传则按单只绝对值插值（兜底，精度低但不报错）。
3. `config.py` 增加 `scoring_weights()` 读取 `scoring.weights` 配置，
   缺失时返回上表默认值。

---

### 2.2 评分集成到数据管道

> **目标**：评分在数据刷新后自动重算，请求路径只读不算，保持响应快。

**需要改动的位置**：
- `providers.py` `adopt_universe(funds, fetched_at)`：新增 `_recompute_scores(funds)` 调用，
  把全集传入 `compute_score` 并把结果写回每个 `Fund` 对象的 `score` / `score_parts`。
- `providers.py` `load_enriched()`：补数回填后对更新过的基金重算分（单只调用，
  不必全集重算）。
- `providers.py` `enrich_fund()`：单只补数后就地重算。
- `providers.py` L225 附近的启发式评分（`if not fund.score`...）：**移除**，改为
  `scoring.compute_score(fund)` 的调用（此处无全集，用兜底模式）。
- `main.py` `query_score()`：基线分改读 `fund.score`（已由新评分写入），不再手写启发式。

**注意**：`_recompute_scores` 要在 `asyncio.to_thread` 里跑，2 万只 × 5 维度计算不能占事件循环。

---

### 2.3 前端接真实 scoreParts + 新手解读文案

> **目标**：卡片和抽屉把空进度条替换成有数字有颜色的维度条，新手看得懂每条是什么意思。

**类型变更**（`src/types.ts`）：
```typescript
export type ScorePart = {
  label: string
  value: number | null   // null = 未获取
  weight: number
  color: string
  raw: string            // 原始数值文字，如 "+18.4%"
  basis: string          // 数据来源说明，如 "AKShare 排行字段"
}
// Fund.scoreParts 改为 ScorePart[]
```

**卡片改动**（`src/App.vue`，`fund-card`）：
- 现在只显示「近 1 年」和「最大回撤」两列数字；改为在 `fund-score` 区域下方展示
  最多 3 条最高权重维度的迷你进度条（宽度按 `value`，`null` 显示灰色「未获取」短条）。
- 第一名卡片额外显示一行「适合：稳健型 / 进取型」标签（来自后端 `reason` 字段升级后的数据）。

**详情抽屉改动**（`src/App.vue`，`detail-drawer`）：
- `drawer-score-row` 评分数字旁边加维度分解列表，每条：`[维度名] [进度条] [数值/未获取] [来源]`。
- 进度条用已有的 `score-line` 样式，接真实 `value`，颜色来自 `part.color`。
- 进度条入场用已有的 `bar-grow` + 各维度错落 delay（已有 CSS）。
- 新增「适合哪类投资者」区块：图标 + 文字，来自后端 `reason` 升级后的字段。

**罗盘卡片**（右栏 `compass-card`）：
- 中心数字已有；四个方向标签「收益/费用/覆盖/来源」改成真实维度名（收益/风险/成本/经理）。
- `score-list` 接真实 `scoreParts`（已有 `v-if="primaryFund.scoreParts.length"`），
  `null` 维度显示「未获取」灰色短条替代空条。

---

### 2.4 reason 字段升级 — 新手一句话结论

> **目标**：每只基金的 `reason` 从「AKShare 排行占位」升级成「一句话结论 + 关键证据数字」。

**后端**（`providers.py` 或新增 `backend/app/reason_builder.py`）：
```python
def build_reason(fund: Fund, score_parts: list[dict]) -> str:
    """根据评分维度拼装一句话结论，例如：
    '近1年收益 +18.4%，回撤控制良好（-12.3%），费率 0.6% 低于同类均值。'
    """
```
- 有数据的维度按权重排序，取前 2～3 条最强的拼成自然语言。
- 没有任何有效维度时回退到「数据待补充，研究时会自动获取关键字段。」
- `adopt_universe` 和 `enrich_fund` 之后调用此函数更新 `fund.reason`。

---

### 2.5 场景预设按钮（一键筛选）

> **目标**：新手不用填写任何条件，点一下「4433 策略」或「低回撤稳健」就能看到合适的候选。

**后端**（`main.py`，`/api/funds/screen` 或在现有 `screen_funds` 增加 `preset` 参数）：
```python
PRESETS = {
    "4433": ScreenRequest(filters=FilterCriteria(maxFee=1.0, riskLevelMax="中高风险"), query="4433"),
    "低回撤稳健": ScreenRequest(filters=FilterCriteria(maxFee=0.8, riskLevelMax="中风险"), query="回撤小 稳健"),
    "科技主题": ScreenRequest(filters=FilterCriteria(fundTypes=["股票型"]), query="科技 半导体 AI"),
}
```
- `GET /api/funds/screen?preset=4433` 返回与现有 `/api/funds?q=...` 相同的 envelope 结构。
- preset 不存在时返回 400，不静默降级。

**前端**（`src/App.vue`，筛选面板顶部）：
- 三个预设按钮：「4433 策略」「低回撤稳健」「科技主题」。
- 点击后调用 `/api/funds/screen?preset=xxx`，结果覆盖 `availableFunds`，
  同时高亮激活的预设按钮（`activePreset` ref）。
- 清除预设按钮回到浏览视图（复用 `clearSearch` 逻辑）。

---

### 2.6 阶段二验证（同上方验证记录）

- [x] `backend/tests/test_scoring.py`（不触网，用 `make_fund` 构造各类基金）：
  - 各维度归一化边界：最大/最小值映射到 100/0。
  - 缺字段（`one_year=None`）不计入加权分母，总分仍正常。
  - `score_parts` 每项都有 `label`/`value`/`weight`/`color`/`raw`/`basis`。
  - `build_reason` 输出非空、包含至少一条数据数字。
  - 4433 预设返回正确的 `ScreenRequest`。
- [x] `test_api.py` 新增：
  - `/api/funds` 返回的基金 `scoreParts` 非空（至少有 1 条有效维度）。
  - `/api/funds/screen?preset=4433` 返回 200 且 items 非空。
  - `/api/funds/screen?preset=不存在` 返回 400。
- [x] 前端 `typecheck` 与生产 build 通过。
- [ ] 本地浏览器交互验收：
  - 卡片显示真实维度条（不全是空条）。
  - 抽屉进度条有颜色有数字。
  - 预设按钮点击后候选列表更新。
- [x] 全量 `pytest`（170 passed）+ `ruff` 通过。

---

## 阶段三：批量补齐关键数据 + 持仓穿透（差异化）

- [x] **3.1 批量补数 worker** (`backend/app/workers/enrichment.py`)
  - 对精选集合（浏览前 200 / 关注列表 / 各主题头部 50）批量补 risk/经理/公司/规模/回撤/波动/主题/成立年限，落 Neon。
  - 并发限流（semaphore，默认 3 并发）+ 指数退避重试 + 断点续跑（`fund.needs_enrichment` 为假跳过）。
  - 进度写 `job_runs`；命令 `python -m app.workers.enrichment`；支持 `--limit N` 只跑前 N 只。
- [x] **3.2 持仓穿透 / 伪分散检测**
  - `enrichment.py` 增 `_fetch_holdings(code)`：雪球/天天基金前十大重仓股（基金代码 → 重仓列表）。
  - 新表 `fund_holding_snapshots`：alembic 迁移，列 `fund_code`/`fetched_at`/`payload_gz`（只存 gzip，避免跨洋大字段坑）。
  - 补数 worker 写持仓；`watchlist` 页展示持仓重叠预警（两只基金共同重仓 ≥ 5 只时提示）；详情抽屉增「持仓穿透」区块。
- [x] **3.3 周期调度**
  - `workers/scheduler.py` 接 APScheduler（加入 `requirements.txt`）：每日凌晨 2 点增量刷新目录 + 批量补数 + 预填充热门 history。
  - `outbox_events` 加简单后台消费者（写审计日志，为未来事件通知做基础）。
- [x] **3.4 数据覆盖度可视化**
  - `GET /api/data/coverage` 返回 `{total, enriched, holdings, navCached}`。
  - 前端侧边栏「数据状态」卡片增进度条：「已补齐 N 只核心字段 / 持仓覆盖 N 只」。
- [x] **3.5 阶段三自动验证**
  - `test_enrichment_worker.py`：持仓幂等/重叠、私有 SQLite + FakeProvider 的重试/跳过、JobRun 进度和 PARTIAL 状态。
  - `test_db.py`：空库迁移升降级、旧库 0003→0004 新建持仓表及回滚；conftest 使用应用 `ensure_schema()` 建临时库。
  - `test_scheduler.py`：暂停启动调度器，确认四个任务注册，绝不实际触发上游任务。
  - 全量 170 项 pytest、ruff、typecheck、生产 build、diff 检查通过。
- [ ] **部署与交互验收**：浏览器预设/抽屉/观察页检查，真实数据源补数、服务重启后回填、生产库迁移和调度器常驻运行验证。自动测试不代表这些运行验收已完成。

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
- `compute_score` 在没有全集时按单只绝对值兜底，精度稍低但不阻塞启动。
