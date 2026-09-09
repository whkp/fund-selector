# FastAPI 无 Docker 的 LLM 接入方案

基金罗盘当前由 FastAPI/Python 服务端通过标准 HTTP(S) 调用 LLM，不依赖 Docker。服务端负责基金事实、数据来源、数据质量门禁和用户明确的硬约束；模型只在服务端筛出的候选池内完成意图理解、研究解释、临时排序和追问。正式版的确定性 `research-score-v1` 尚未替换当前模型排序分，因此当前模型返回的 `score` 只能视为研究展示分，不能视为经过回测验证的投资评分。

## Provider

| Provider | 适用 | 默认状态 |
|---|---|---|
| `openai-compatible` | OpenAI、DeepSeek、通义千问兼容网关等 | 推荐，服务端环境变量配置 |
| `ollama` | 本机原生模型 | 可选，不需要 Docker |

生产配置也可以统一写入 `config/fund-compass.json`，无需设置多组环境变量：

```json
{
  "llm": {
    "provider": "openai-compatible",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "your-secret",
    "model": "deepseek-chat",
    "timeout_seconds": 45
  },
  "knowledge": {"path": "./data/knowledge"}
}
```

模板是 `config/fund-compass.example.json`，真实配置文件已被 Git 忽略。也可以使用环境变量覆盖单个字段，环境变量优先级更高。

环境变量配置示例：

```bash
export FUND_COMPASS_LLM_PROVIDER=openai-compatible
export FUND_COMPASS_LLM_BASE_URL=https://provider.example/v1
export FUND_COMPASS_LLM_API_KEY=your-secret
export FUND_COMPASS_LLM_MODEL=your-model
FUND_COMPASS_MODE=REFERENCE .venv/bin/python backend/run.py
```

页面只读取 `GET /api/ai/status` 的服务端状态，同时提供会话级 BYOK 设置入口。用户填写的 Key 只随本次研究请求发送，不写入服务器配置、研究记录、日志或浏览器持久化存储。

模型调用失败、超时或结构化输出不符合 Schema 时，研究请求返回明确错误；系统不会回退为规则推荐或模拟结果。基金事实、硬约束和数据来源始终由服务端真实数据管道负责。

## 当前实际研究链路（2026-09-10）

当前研究接口是“服务端预处理 + 单次模型调用 + 服务端校验”，不是把用户原话直接转发给模型，也不是多轮 Agent。一次 `POST /api/recommendations/runs` 的执行顺序固定为：

```text
用户目标与筛选条件
  -> 服务端硬约束筛选（基金类型、风险、费率、开放申购、成立年限）
  -> 读取最多 60 只候选基金
  -> KnowledgeBase.search(query, limit=5)
  -> 一次 POST {base_url}/chat/completions
  -> Pydantic JSON Schema 校验
  -> 候选基金代码白名单、去重和数量校验
  -> 将模型解释绑定回服务端基金事实
  -> 生成 Recommendation Run、候选和 Trace
```

模型请求由 `backend/app/llm.py::LLMService.research()` 组装，包含：

- system 约束：只能使用候选事实和知识片段，不得创建基金、补全缺失字段、编造数字、预测收益或生成交易指令；
- user JSON：`query`、最多 60 只候选基金的压缩字段、最多 5 个知识片段和 `limit`；
- `temperature: 0.2`；
- `response_format: {"type": "json_object"}`；
- OpenAI-compatible 或 Ollama 的单个 `/chat/completions` 请求。

模型输出结构为 `intent`、`themes`、`ambiguities`、`summary`、`ranking` 和 `followUpQuestions`。其中每个 `ranking` 项包含 `fundCode`、`score`、`fit`、`reason` 和 `riskFlags`。服务端拒绝格式错误、候选池之外的基金代码和无效字段；重复代码会去重，结果数量会截断到请求上限。服务端随后使用原始基金对象重新填充名称、净值、来源、快照和数据质量字段，模型不能覆盖这些事实。

当前明确不包含：模型自主调用 AKShare、联网搜索、工具调用循环、多轮追问后再次调用模型、模型复核模型、流式输出，或在 LLM 失败时回退成规则推荐。未配置模型、上游超时、HTTP 错误或 JSON/Schema/候选代码校验失败时，接口返回可解释的失败状态，事实查询仍可用。

当前知识库为可选的本地 Markdown 词法检索（未配置时传空列表），并非向量 RAG 或联网检索。后续接入 pgvector、Milvus 或外部检索服务时，应保持同一 `KnowledgeBase` 协议，并继续由服务端控制候选和证据边界。

## 知识库接口

研究请求会先调用 `KnowledgeBase.search(query, limit)`，将检索片段作为带来源的上下文传给模型。当前内置 `local-markdown` 适配器：设置 `FUND_COMPASS_KNOWLEDGE_BASE_PATH` 指向 Markdown 目录即可使用。

```text
GET  /api/knowledge/status
POST /api/knowledge/search  {"query":"长期定投","limit":5}
```

后续接入 pgvector、Milvus 或外部 RAG 服务时，只需实现同一 `KnowledgeBase` 协议，不改变研究路由和模型输出 Schema。

## Agent 边界

Agent 可以：

- 从自然语言提取期限、风险、主题、费用和流动性偏好；
- 调用只读基金搜索、指标、比较和质量工具；
- 解释确定性筛选或仓位复核信号；
- 生成待确认问题和研究摘要。

Agent 不可以：

- 编造基金、净值、收益、来源或公告；
- 修改风险画像和服务端硬约束；
- 根据短期涨跌自行生成加仓/卖出指令；
- 访问任意用户 URL、拼接 SQL 或直接调用 AKShare。

真实云端模型上线前仍需完成密钥管理、host allowlist、调用限额、隐私提示、日志脱敏和成本监控。当前 Adapter 使用 OpenAI Chat Completions 兼容协议，并要求模型返回结构化 JSON；服务端会校验基金代码只能来自真实候选集合。

## 公网短期 BYOK

页面的“设置我的模型”支持 `openai-compatible` 和 `ollama`。API Key 只保存在当前 Vue 页面内存，并在 `POST /api/recommendations/runs` 的本次请求中发送；后端创建一次性 `LLMService`，请求结束后不写入全局配置、数据库、Recommendation Run、Trace、日志或浏览器存储。刷新或离开页面后 Key 从前端内存消失。

公网会话只允许 HTTPS 和服务端白名单域名；本机 Ollama 地址默认拒绝，开发者本地测试可在 `config/fund-compass.json` 设置 `app.allow_local_llm=true`。当前模式不支持账号级持久化 Key，正式版需要用户认证、KMS/Secret Manager 加密和用户级限流。
