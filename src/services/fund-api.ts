import type { Fund } from '../types'
import { request, TIMEOUT_MS } from './http'

export type ResearchRun = {
  runId: string
  status: string
  mode: string
  modelVersion: string
  conversationId?: string
  interpretation: { provider: string; status: string; intent?: string; themes?: string[]; ambiguities?: string[] }
  summary: string
  knowledgeRefs: Array<{ chunk_id: string; title: string; source: string }>
  candidates: Array<{ fund: Fund; rank: number; analysis?: string; riskFlags?: string[]; recommendationScore: number }>
  trace: Array<{ event: string; title: string; detail: string; status: string }>
  disclaimer: string
}

export type AIStatus = {
  provider: string
  model: string
  status: 'READY' | 'NOT_CONFIGURED'
  credentials: 'server-side'
  configurationSource?: 'environment' | 'config-file' | 'default' | 'runtime-memory'
  message: string
}

export type SessionLLMConfig = {
  provider: 'openai-compatible' | 'ollama'
  baseUrl: string
  apiKey: string
  model: string
  timeoutSeconds?: number
}

export type MarketQuote = {
  code: string
  name: string
  venue: string
  marketPrice: number
  previousClose: number
  changePercent: number
  amountWan: number
  turnoverPercent: number
  asOf: string
  sourceName: string
  sourceType: 'EXCHANGE_QUOTE'
  trustLevel: 'LOW'
  freshnessStatus: 'ACTIVE' | 'STALE'
  snapshotId: string
  staleReason?: string
}

export type MarketSourceStatus = {
  sourceName: string
  sourceType: string
  trustLevel: string
  status: 'ACTIVE' | 'PENDING' | 'DEGRADED' | 'STALE' | 'ERROR' | 'NOT_CONFIGURED'
  configured: boolean
  licenseScope: string
  refreshInterval?: string
  staleAfter?: string
  lastAttemptAt?: string
  lastSuccessAt?: string
  lastError?: string
}

export type MarketQuoteResponse = {
  items: MarketQuote[]
  status: MarketSourceStatus
  disclaimer: string
}

type FundListResponse = { items: Fund[] }
type WatchlistResponse = { items: Array<{ fund: Fund }> }

export async function fetchFunds(): Promise<Fund[]> {
  // 首次请求会触发服务端抓一遍 AKShare 全量基金列表，比普通读接口慢得多。
  const payload = await request<FundListResponse>('/funds', undefined, {
    timeoutMs: TIMEOUT_MS.upstream,
  })
  return payload.items
}

export type FundHistoryResponse = {
  fundCode: string
  period: string
  items: Array<{ date: string; nav: number; dailyChange: number }>
  metrics?: Record<string, number | string>
  metricStatus?: string
  metricVersion: string
  mode?: 'REFERENCE' | 'PRODUCTION'
  sourceType?: string
  trustLevel?: string
  sourceStatus?: MarketSourceStatus
}

export async function fetchFundHistory(code: string, period = '1年'): Promise<FundHistoryResponse> {
  return request<FundHistoryResponse>(
    `/funds/${code}/history?period=${encodeURIComponent(period)}`,
    undefined,
    { timeoutMs: TIMEOUT_MS.upstream },
  )
}

export async function fetchAIStatus(): Promise<AIStatus> {
  return request<AIStatus>('/ai/status')
}

export async function createResearchRun(query: string, maxFee: number, riskLevelMax: string, options?: {
  fundTypes?: string[]
  requireOpen?: boolean
  minimumInceptionYears?: number
  llm?: SessionLLMConfig
  /** 传入已存在的对话 ID 即为「追问」，留空则由服务端新建一个对话。 */
  conversationId?: string
}): Promise<ResearchRun> {
  return request<ResearchRun>('/recommendations/runs', {
    method: 'POST',
    body: JSON.stringify({
      query,
      limit: 10,
      conversationId: options?.conversationId ?? '',
      filters: {
        riskLevelMax,
        maxFee,
        requireOpen: options?.requireOpen ?? true,
        fundTypes: options?.fundTypes ?? [],
        minimumInceptionYears: options?.minimumInceptionYears ?? 0,
      },
      ...(options?.llm ? { llm: options.llm } : {}),
    }),
  }, { timeoutMs: TIMEOUT_MS.research })
}

export async function fetchWatchlist(): Promise<Fund[]> {
  const payload = await request<WatchlistResponse>('/watchlist/items')
  return payload.items.map((item) => item.fund)
}

export async function fetchMarketQuotes(): Promise<MarketQuoteResponse> {
  return request<MarketQuoteResponse>('/market/etf-quotes', undefined, {
    timeoutMs: TIMEOUT_MS.upstream,
  })
}

export async function refreshMarketQuotes(): Promise<MarketQuoteResponse> {
  return request<MarketQuoteResponse>('/market/etf-quotes/refresh', { method: 'POST' }, {
    timeoutMs: TIMEOUT_MS.upstream,
  })
}

export async function addWatchlistItem(fund: Fund): Promise<void> {
  await request('/watchlist/items', {
    method: 'POST',
    body: JSON.stringify({ fundCode: fund.code, reasonTags: fund.tags }),
  })
}

export async function removeWatchlistItem(fund: Fund): Promise<void> {
  await request(`/watchlist/items/${fund.code}`, { method: 'DELETE' })
}
