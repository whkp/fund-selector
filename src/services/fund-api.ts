import type { Fund } from '../types'

const apiBase = import.meta.env.VITE_API_BASE ?? '/api'

export type ResearchRun = {
  runId: string
  status: string
  mode: string
  modelVersion: string
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
  configurationSource?: 'environment' | 'runtime-memory'
  message: string
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { error?: { message?: string } } | null
    const detail = payload?.error?.message ?? (payload as { detail?: { message?: string } } | null)?.detail?.message
    throw new Error(detail ?? `API request failed (${response.status})`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export async function fetchFunds(): Promise<Fund[]> {
  const payload = await request<FundListResponse>('/funds')
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
  return request<FundHistoryResponse>(`/funds/${code}/history?period=${encodeURIComponent(period)}`)
}

export async function fetchAIStatus(): Promise<AIStatus> {
  return request<AIStatus>('/ai/status')
}

export async function createResearchRun(query: string, maxFee: number, riskLevelMax: string): Promise<ResearchRun> {
  return request<ResearchRun>('/recommendations/runs', {
    method: 'POST',
    body: JSON.stringify({
      query,
      limit: 10,
      filters: { riskLevelMax, maxFee, requireOpen: true },
    }),
  })
}

export async function fetchWatchlist(): Promise<Fund[]> {
  const payload = await request<WatchlistResponse>('/watchlist/items')
  return payload.items.map((item) => item.fund)
}

export async function fetchMarketQuotes(): Promise<MarketQuoteResponse> {
  return request<MarketQuoteResponse>('/market/etf-quotes')
}

export async function refreshMarketQuotes(): Promise<MarketQuoteResponse> {
  return request<MarketQuoteResponse>('/market/etf-quotes/refresh', { method: 'POST' })
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
