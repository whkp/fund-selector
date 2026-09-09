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

```bash
npm install --cache /private/tmp/fund-compass-npm-cache
```

在一个终端启动 FastAPI（编辑 `config/fund-compass.json` 即可完成常规配置；未配置模型时基金浏览仍可用，但点击研究会返回配置提示）：

```bash
FUND_COMPASS_MODE=REFERENCE \
FUND_COMPASS_AKSHARE_ENABLED=true \
.venv/bin/python backend/run.py
```

配置模板见 `config/fund-compass.example.json`。实际的 `config/fund-compass.json` 已加入 `.gitignore`，API Key 不会被提交。只有在需要临时覆盖配置或部署到托管平台时，才使用 `FUND_COMPASS_*` 环境变量；环境变量优先级高于配置文件。

在另一个终端启动前端：

```bash
npm run dev -- --port 4173
```

浏览器访问 `http://127.0.0.1:4173/`，前端会通过 Vite 代理请求 `http://127.0.0.1:8080` 的 FastAPI。API 或 AKShare 不可用时，页面显示错误/空状态，不会展示模拟基金。

Reference 模式默认只读。公网部署至少应保持 `FUND_COMPASS_PUBLIC_WRITE_ENABLED=false`，并将 `FUND_COMPASS_PUBLIC_RESEARCH_ENABLED` 设为 `false`，待认证和限流完成后再开放匿名研究。

`PRODUCTION` 模式不会因为修改模式变量就自动获得生产数据。只有在已接入并审核生产数据源后，才设置 `FUND_COMPASS_PRODUCTION_DATA_READY=true`；否则 API 会返回 `PRODUCTION_DATA_NOT_READY`。

AKShare 数据源状态使用 `GET /api/data-sources/status` 获取；基金目录/排行刷新使用 `POST /api/data/funds/refresh`。

数据库骨架已包含 Alembic 迁移和独立基金目录 Worker。首次初始化本地数据库：

```bash
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/python -m app.workers.scheduler
```

Worker 会把目录结果写入 raw/profile 快照、任务表和 Outbox；相同 Provider 内容 hash 的重复同步会返回 `UNCHANGED`，不会重复写入同一份快照。

## 验证

```bash
.venv/bin/python -m pytest -q backend/tests
npm run typecheck
npm run build
git diff --check
```

构建产物位于 `dist/`。

## GitHub Pages 与公网后端

仓库包含 `.github/workflows/deploy-pages.yml`。推送 `main` 后，GitHub Actions 会将 Vue 前端部署到：

```text
https://whkp.github.io/fund-selector/
```

GitHub Pages 只能运行静态前端，不能承载 FastAPI、AKShare 或 LLM API。要让手机端完整使用真实基金数据和大模型研究，需要先部署后端；仓库提供了 `render.yaml`，可在 Render 以 Python 原生进程部署，不需要 Docker。部署后将 Render 的 HTTPS 地址设置为 GitHub 仓库变量 `VITE_API_BASE`（值为例如 `https://fund-compass-api.onrender.com/api`），再重新运行 Pages 工作流。后端的 LLM Key 只配置在 Render 的 Secret 环境变量中，不放到前端或 GitHub Pages。
