export type ScorePart = {
  label: string
  value: number | null   // null = 未获取
  weight: number
  color: string
  raw: string            // 原始数值文字，如 "+18.4%"
  basis: string          // 数据来源说明，如 "AKShare 排行字段"
}

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
  scoreParts: ScorePart[]
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
