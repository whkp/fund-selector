export type Fund = {
  id: string
  code: string
  name: string
  shortName: string
  type: string
  risk: string
  manager: string
  managerYears: number | null
  company: string
  theme: string
  nav: number | null
  navDate: string
  ytd: number | null
  oneYear: number | null
  volatility: number | null
  drawdown: number | null
  fee: number | null
  scale: number | null
  inception: number | null
  score: number
  scoreParts: { label: string; value: number; color: string }[]
  reason: string
  caveat: string
  highlights: string[]
  status: string
  source: string
  snapshot: string
  chart: number[]
  tags: string[]
  intake: string
  qualityStatus?: string
  navSourceType?: string
  navTrustLevel?: string
  navFreshness?: string
}
