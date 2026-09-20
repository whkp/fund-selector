<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Activity, ArrowDownUp, ArrowUpRight, BarChart3, BellRing, Bot, CalendarClock,
  Check, ChevronDown, ChevronRight, CircleAlert, Clock3, Compass, Database,
  FileSearch, Filter, Info, Landmark, LineChart, ListFilter, Loader2, LogOut, Menu, MoreHorizontal,
  Plus, Radio, RefreshCw, Search, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, Star, X
} from 'lucide-vue-next'
import type { Fund } from './types'
import {
  addWatchlistItem, createResearchRun, fetchAIStatus, fetchFunds, fetchFundHistory, fetchMarketQuotes, fetchRunTrace,
  searchFunds,
  fetchWatchlist, refreshMarketQuotes, removeWatchlistItem, type MarketQuote, type MarketQuoteResponse,
  type AIStatus, type FundHistoryResponse, type MarketSourceStatus, type ResearchRun, type RunTrace, type SessionLLMConfig
} from './services/fund-api'
import LoginView from './views/LoginView.vue'
import {
  deleteConversation, fetchConversation, fetchConversations, fetchMe,
  logout as logoutSession, type AuthUser, type Conversation, type ConversationMessage,
} from './services/auth'
import { onUnauthorized } from './services/http'

const activeNav = ref('基金筛选')
const query = ref('准备定投 3 年，关注新能源和高端制造，但不希望波动太大，费用尽量低。')
const fundType = ref('不限')
const riskLimit = ref('中高风险')
const theme = ref('新能源 / 制造')
const maxFee = ref(1.2)
const requireOpen = ref(true)
const checked = ref(['期限 3 年', '中等风险', '定投方式', '开放申购'])
const availableFunds = ref<Fund[]>([])
// 完整基金全集，只在初始加载/手动刷新时更新。研究结果会覆盖 availableFunds，
// 但类型选项必须始终基于全集，否则跑完一次研究筛选条就塌了。
const fundUniverse = ref<Fund[]>([])
const watchlist = ref<Fund[]>([])
const compareList = ref<Fund[]>([])
const selectedFund = ref<Fund | null>(null)
const selectedHistory = ref<FundHistoryResponse | null>(null)
const historyLoading = ref(false)
const traceOpen = ref(false)
const mobileMenuOpen = ref(false)
const noteDraft = ref('')
const toast = ref('')
const apiConnected = ref(false)
const loadError = ref('')
const isResearchRunning = ref(false)
const latestRun = ref<ResearchRun | null>(null)
// 研究进行中的实时思考过程：轮询 /runs/{id}/trace 拿到 agent 的每一步。
const liveTrace = ref<RunTrace['trace']>([])
let traceTimer: number | undefined
const aiStatus = ref<AIStatus | null>(null)
const aiSettingsOpen = ref(false)
const sessionLLM = ref<SessionLLMConfig>({
  provider: 'openai-compatible', baseUrl: 'https://api.openai.com/v1', apiKey: '', model: 'gpt-4o-mini', timeoutSeconds: 45,
})
const marketQuotes = ref<MarketQuote[]>([])
const marketStatus = ref<MarketSourceStatus | null>(null)
const marketDisclaimer = ref('场内交易价格仅作 ETF/LOF 行情参考，不等于基金正式净值。')
const isMarketRefreshing = ref(false)
const isMarketLoading = ref(false)
let marketTimer: number | undefined

// 登录态。authReady 为 false 时先确认令牌，避免闪一下主界面再跳回登录页。
const authReady = ref(false)
const currentUser = ref<AuthUser | null>(null)
// 复盘日志：当前账号的历史对话
const conversations = ref<Conversation[]>([])
const conversationMessages = ref<ConversationMessage[]>([])
const activeConversationId = ref('')
const conversationLoading = ref(false)

const navigation = [
  { label: '基金筛选', icon: Compass }, { label: '实时行情', icon: LineChart }, { label: '对比台', icon: ArrowDownUp },
  { label: '我的观察', icon: Star }, { label: '目标与风险', icon: SlidersHorizontal },
  { label: '复盘日志', icon: CalendarClock }
]

// AKShare 公开排行不返回风险等级、申购状态与成立年限，这些字段会落成"未获取"。
// 与后端 eligibility() 保持同一套规则：缺失不排除，只如实标注未核验。
const UNKNOWN_VALUES = new Set(['', '未获取', '未知', '未标注', '待核', 'none', 'null', 'n/a', '-'])
const TYPE_SEPARATORS = /[-—－–/／·|]/

function isKnown(value: unknown): boolean {
  if (value === null || value === undefined) return false
  return !UNKNOWN_VALUES.has(String(value).trim().toLowerCase())
}

/** 数据侧是 "混合型-偏股"，界面给的是 "混合型"，所以按切分后的根段比较。 */
function matchesFundType(fundTypeValue: unknown, wanted: string): boolean {
  if (!wanted || wanted === '不限') return true
  const source = String(fundTypeValue ?? '').trim()
  if (!source) return false
  const segments = source.split(TYPE_SEPARATORS).map((segment) => segment.trim()).filter(Boolean)
  return source === wanted || source.startsWith(wanted) || segments.includes(wanted)
}

function fundTypeRoot(fundTypeValue: unknown): string {
  return String(fundTypeValue ?? '').split(TYPE_SEPARATORS)[0]?.trim() ?? ''
}

/** 类型选项由真实数据生成：数据源里没有的类型不再画成死按钮。 */
const fundTypeOptions = computed(() => {
  const tally = new Map<string, number>()
  for (const fund of availableFunds.value) {
    const root = fundTypeRoot(fund.type)
    if (!root) continue
    tally.set(root, (tally.get(root) ?? 0) + 1)
  }
  const options = [...tally.entries()].sort((a, b) => b[1] - a[1]).map(([label, count]) => ({ label, count }))
  return [{ label: '不限', count: availableFunds.value.length }, ...options]
})

/**
 * 主题筛选：AKShare 排行没有主题字段（theme 全是「未标注」占位），
 * 这里按基金名称里的主题词做包含匹配，词表按公开基金命名惯例整理。
 * 「均衡配置 / 不限主题」不参与过滤。
 */
const THEME_KEYWORDS: Record<string, string[]> = {
  '新能源 / 制造': ['新能源', '光伏', '锂电', '电池', '能源', '环保', '电力', '车', '制造', '工业', '材料', '碳中和', '装备'],
  '科技成长': ['科技', '半导体', '芯片', '电子', '信息', '计算机', '互联网', '人工智能', '智能', '软件', '通信', '传媒', '游戏', '数字', '5G', '算力', '机器人', '创新', '科创'],
}

function matchesTheme(fund: Fund, wanted: string): boolean {
  const keywords = THEME_KEYWORDS[wanted]
  if (!keywords) return true
  const haystack = `${fund.name} ${fund.type}`
  return keywords.some((keyword) => haystack.includes(keyword))
}

const emptyHint = computed(() => {
  if (latestRun.value && !displayedFunds.value.length) {
    const themeActive = Boolean(THEME_KEYWORDS[theme.value])
    const themeHits = themeActive && availableFunds.value.some((fund) => matchesTheme(fund, theme.value))
    if (themeActive && !themeHits) {
      return `当前研究候选里没有「${theme.value}」主题的基金。可在左侧切回不限主题，或换一个研究目标重新研究。`
    }
    return '模型未从当前真实候选范围中返回结果，请调整目标或补充条件。'
  }
  if (loadError.value) return loadError.value
  if (fundType.value !== '不限' && fundUniverse.value.length) {
    const inUniverse = fundUniverse.value.some((fund) => matchesFundType(fund.type, fundType.value))
    return inUniverse
      ? `本次研究候选里没有「${fundType.value}」类型的基金，可在左侧切回不限。`
      : `当前数据源没有「${fundType.value}」类型的基金。AKShare 公开排行只提供收益前 500 名，类型分布见左侧筛选条。`
  }
  return '正在从 AKShare 同步公开参考数据。'
})

const displayedFunds = computed(() => {
  const riskRank: Record<string, number> = { '低风险': 1, '中低风险': 2, '中风险': 3, '中高风险': 4, '高风险': 5 }
  return availableFunds.value
    .filter((fund) => matchesFundType(fund.type, fundType.value))
    .filter((fund) => matchesTheme(fund, theme.value))
    .filter((fund) => !isKnown(fund.risk) || riskRank[fund.risk] <= riskRank[riskLimit.value])
    .filter((fund) => fund.fee === null || fund.fee <= maxFee.value)
    .slice(0, 30)
})

// ---- 按名称/代码直接搜索：不走研究流程，从全量 n-gram 索引里召回任意基金 ----
const searchKeyword = ref('')
const searchActive = ref(false)
const isSearching = ref(false)
let searchTimer: number | undefined

async function runSearch() {
  const keyword = searchKeyword.value.trim()
  if (!keyword) {
    clearSearch()
    return
  }
  isSearching.value = true
  try {
    const hits = await searchFunds(keyword)
    if (searchKeyword.value.trim() !== keyword) return // 关键词已变，丢弃过期结果
    searchActive.value = true
    // 搜索结果按关键词直接命中，重置类型/主题避免旧筛选把结果滤空
    fundType.value = '不限'
    theme.value = '不限主题'
    availableFunds.value = hits
    notify(hits.length
      ? `找到 ${hits.length} 只与「${keyword}」相关的基金`
      : `没有找到与「${keyword}」相关的基金，试试基金全名或 6 位代码`)
  } catch {
    notify('搜索失败，请稍后重试')
  } finally {
    isSearching.value = false
  }
}

function onSearchInput() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(runSearch, 450)
}

function clearSearch() {
  window.clearTimeout(searchTimer)
  searchKeyword.value = ''
  if (!searchActive.value) return
  searchActive.value = false
  // 回到浏览视图（前 200 只）；若之前在看研究结果则一并清掉
  latestRun.value = null
  availableFunds.value = fundUniverse.value
}

/** 数据源给不出风险等级时（候选全部"未获取"），风险上限条件实际不参与筛选，界面要如实说明。 */
const riskFilterUsable = computed(() => displayedFunds.value.some((fund) => isKnown(fund.risk)))

const primaryFund = computed(() => displayedFunds.value[0] ?? availableFunds.value[0] ?? null)
const activeTrace = computed(() => latestRun.value?.trace ?? [])
const marketStatusLabel = computed(() => {
  if (!apiConnected.value) return '基金数据不可用'
  if (!marketStatus.value) return '行情服务连接中'
  const labels: Record<string, string> = {
    ACTIVE: 'ETF 行情已刷新', PENDING: '行情等待首个快照', DEGRADED: '行情使用最近快照',
    STALE: '行情快照已过期', ERROR: '行情刷新失败', NOT_CONFIGURED: '行情源未配置'
  }
  return labels[marketStatus.value.status] ?? marketStatus.value.status
})
const aiModeLabel = computed(() => {
  if (!apiConnected.value) return '本地降级'
  if (!aiStatus.value) return '研究引擎连接中'
  if (aiStatus.value.status === 'NOT_CONFIGURED') return '模型未配置'
  return `${aiStatus.value.provider} · ${aiStatus.value.model}`
})
const hasSessionLLM = computed(() => Boolean(sessionLLM.value.apiKey.trim()))
const userInitial = computed(() =>
  (currentUser.value?.displayName || currentUser.value?.email || '?').trim().slice(0, 1).toUpperCase()
)
const marketAsOf = computed(() => marketStatus.value?.lastSuccessAt ? formatTime(marketStatus.value.lastSuccessAt) : '尚无成功快照')
const dataModeLabel = computed(() => {
  const fund = availableFunds.value[0]
  if (!apiConnected.value) return 'AKShare 未连接'
  return `${fund?.navSourceType ?? 'AKSHARE_PUBLIC'} · ${fund?.navTrustLevel ?? 'LOW'}`
})
const dataModeDisclaimer = computed(() => apiConnected.value
  ? '候选来自 AKShare 公开参考数据；未提供的字段会明确显示为“未获取”。'
  : '未连接到数据服务，页面不会以本地模拟基金替代真实数据。')

async function loadConversations() {
  try {
    conversations.value = await fetchConversations()
  } catch {
    conversations.value = []
  }
}

async function openConversation(id: string) {
  // 再次点击已展开的记录即收起，省得再去点关闭。
  if (activeConversationId.value === id && conversationMessages.value.length) {
    activeConversationId.value = ''
    conversationMessages.value = []
    return
  }
  activeConversationId.value = id
  conversationLoading.value = true
  try {
    conversationMessages.value = (await fetchConversation(id)).messages
  } catch {
    conversationMessages.value = []
    notify('读取研究记录失败')
  } finally {
    conversationLoading.value = false
  }
}

async function removeConversation(id: string) {
  try {
    await deleteConversation(id)
    conversations.value = conversations.value.filter((item) => item.id !== id)
    if (activeConversationId.value === id) {
      activeConversationId.value = ''
      conversationMessages.value = []
    }
    notify('已删除该研究记录')
  } catch {
    notify('删除失败，请稍后重试')
  }
}

function startNewConversation() {
  activeConversationId.value = ''
  conversationMessages.value = []
  latestRun.value = null
  activeNav.value = '基金筛选'
  notify('已开始一轮新的研究')
}

async function handleAuthenticated(user: AuthUser) {
  currentUser.value = user
  await bootstrap()
}

async function handleLogout() {
  await logoutSession()
  currentUser.value = null
  watchlist.value = []
  conversations.value = []
  conversationMessages.value = []
  activeConversationId.value = ''
  latestRun.value = null
  notify('已退出登录')
}

function notify(message: string) {
  toast.value = message
  window.setTimeout(() => { toast.value = '' }, 2600)
}

async function toggleWatch(fund: Fund) {
  const existing = watchlist.value.find((item) => item.id === fund.id)
  try {
    if (apiConnected.value) {
      if (existing) await removeWatchlistItem(fund)
      else await addWatchlistItem(fund)
    }
    watchlist.value = existing ? watchlist.value.filter((item) => item.id !== fund.id) : [...watchlist.value, fund]
    notify(existing ? `已从观察列表移除「${fund.shortName}」` : `已加入观察列表：${fund.shortName}`)
  } catch {
    notify('观察列表保存失败，已保留当前页面状态')
  }
}

function toggleCompare(fund: Fund) {
  const existing = compareList.value.find((item) => item.id === fund.id)
  if (existing) {
    compareList.value = compareList.value.filter((item) => item.id !== fund.id)
    notify(`已移出对比：${fund.shortName}`)
    return
  }
  if (compareList.value.length >= 4) {
    notify('对比台最多同时放入 4 只基金')
    return
  }
  compareList.value = [...compareList.value, fund]
  notify(`已加入对比：${fund.shortName}`)
}

function resetResearch() {
  query.value = '准备定投 3 年，关注新能源和高端制造，但不希望波动太大，费用尽量低。'
  fundType.value = '不限'
  riskLimit.value = '中高风险'
  theme.value = '新能源 / 制造'
  maxFee.value = 1.2
  requireOpen.value = true
  notify('已恢复为本次研究条件')
}

function stopTracePolling() {
  if (traceTimer !== undefined) {
    window.clearInterval(traceTimer)
    traceTimer = undefined
  }
}

/** 研究进行中轮询服务端轨迹，让用户看到 agent 每一步在做什么。 */
function startTracePolling(runId: string) {
  stopTracePolling()
  liveTrace.value = []
  traceTimer = window.setInterval(() => {
    fetchRunTrace(runId).then((payload) => {
      liveTrace.value = payload.trace
    }).catch(() => {
      /* 占位记录尚未建好（404）或瞬时网络抖动：静默，下一轮再取 */
    })
  }, 1200)
}

async function runResearch() {
  if (isResearchRunning.value) return
  if (!apiConnected.value) {
    notify('基金数据服务未连接，无法执行研究')
    return
  }
  isResearchRunning.value = true
  // runId 由前端生成：服务端收到后立即建 RUNNING 占位并随 agent 推进更新 trace。
  const runId = `run_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
  startTracePolling(runId)
  try {
    const run = await createResearchRun(query.value, maxFee.value, riskLimit.value, {
      fundTypes: fundType.value === '不限' ? [] : [fundType.value],
      requireOpen: requireOpen.value,
      minimumInceptionYears: checked.value.includes('期限 3 年') ? 3 : 0,
      llm: hasSessionLLM.value ? sessionLLM.value : undefined,
      // 已有对话即为追问，会接着上一轮存进同一条记录。
      conversationId: activeConversationId.value,
      runId,
    })
    latestRun.value = run
    activeConversationId.value = run.conversationId ?? ''
    availableFunds.value = run.candidates.map((candidate) => candidate.fund)
    await loadConversations()
    notify(run.candidates.length
      ? `研究完成：通过硬约束的候选 ${run.candidates.length} 只`
      : '研究完成：未找到契合的候选，结论见上方摘要')
  } catch (error) {
    notify(error instanceof Error ? error.message : '研究请求失败')
  } finally {
    stopTracePolling()
    liveTrace.value = []
    isResearchRunning.value = false
  }
}

function clearSessionLLM() {
  sessionLLM.value = { ...sessionLLM.value, apiKey: '' }
  aiSettingsOpen.value = false
  notify('已清除本次会话模型 Key')
}

async function openFund(fund: Fund) {
  selectedFund.value = fund
  selectedHistory.value = null
  if (!apiConnected.value) return
  historyLoading.value = true
  try {
    selectedHistory.value = await fetchFundHistory(fund.code)
  } catch {
    selectedHistory.value = null
  } finally {
    historyLoading.value = false
  }
}

function addNote() {
  if (!noteDraft.value.trim()) return
  notify('备注仅保存在当前服务进程，重启后会清除')
  noteDraft.value = ''
}

function fmtPercent(value: number | null | undefined) { return typeof value === 'number' ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : '--' }
/** 涨跌着色：中国市场惯例红涨绿跌，非数值不着色。 */
function metricTone(value: unknown): 'up' | 'down' | '' {
  return typeof value === 'number' ? (value >= 0 ? 'up' : 'down') : ''
}
function fmtNumber(value: number | null | undefined, digits = 2) { return typeof value === 'number' ? value.toFixed(digits) : '--' }
function fmtFundMetric(value: unknown, digits = 2) { return typeof value === 'number' ? value.toFixed(digits) : '--' }
function fmtPrice(value: number) { return value.toFixed(3) }
function fmtAmount(value: number) { return `${value >= 10000 ? (value / 10000).toFixed(2) + ' 亿' : value.toFixed(0) + ' 万'}` }
function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '--'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
  }).format(date)
}
function isWatching(fund: Fund) { return Boolean(watchlist.value.find((item) => item.id === fund.id)) }
function isComparing(fund: Fund) { return Boolean(compareList.value.find((item) => item.id === fund.id)) }
const selectedChart = computed(() => {
  const history = selectedHistory.value?.items ?? []
  if (history.length > 1) {
    const values = history.map((item) => item.nav)
    const min = Math.min(...values)
    const max = Math.max(...values)
    const span = max - min || 1
    return values.map((value) => 16 + ((value - min) / span) * 76)
  }
  return []
})
function chartLine(fund: Fund | null) {
  if (!fund) return ''
  const points = selectedChart.value
  if (!points.length) return ''
  return `M 0,${104 - points[0]} ${points.map((value, index) => `L ${(index / Math.max(points.length - 1, 1)) * 300},${104 - value}`).join(' ')}`
}
function chartArea(fund: Fund | null) {
  const line = chartLine(fund)
  return line ? `${line} L 300,104 L 0,104 Z` : ''
}

function applyMarketResponse(response: MarketQuoteResponse) {
  marketQuotes.value = response.items
  marketStatus.value = response.status
  marketDisclaimer.value = response.disclaimer
}

async function loadMarketQuotes() {
  if (!apiConnected.value || isMarketLoading.value) return
  isMarketLoading.value = true
  try {
    applyMarketResponse(await fetchMarketQuotes())
  } catch {
    marketStatus.value = marketStatus.value ? { ...marketStatus.value, status: 'DEGRADED', lastError: '浏览器未能读取最新行情快照' } : null
  } finally {
    isMarketLoading.value = false
  }
}

async function refreshLiveMarket() {
  if (isMarketRefreshing.value || !apiConnected.value) return
  isMarketRefreshing.value = true
  try {
    applyMarketResponse(await refreshMarketQuotes())
    notify('已请求刷新 ETF 场内行情')
  } catch {
    await loadMarketQuotes()
    notify('行情刷新失败，已保留最近成功快照')
  } finally {
    isMarketRefreshing.value = false
  }
}

async function bootstrap() {
  try {
    const [remoteFunds, remoteWatchlist, remoteAIStatus] = await Promise.all([fetchFunds(), fetchWatchlist(), fetchAIStatus()])
    availableFunds.value = remoteFunds
    fundUniverse.value = remoteFunds
    watchlist.value = remoteWatchlist
    aiStatus.value = remoteAIStatus
    apiConnected.value = true
    loadError.value = remoteFunds.length ? '' : 'AKShare 当前未返回可展示的基金数据。请检查数据源状态后刷新。'
    await loadMarketQuotes()
    await loadConversations()
  } catch {
    apiConnected.value = false
    loadError.value = '无法连接基金数据服务。请启动 FastAPI 并确认 AKShare 数据源可用。'
  }
}

onMounted(async () => {
  // 令牌一旦失效就把界面切回登录页，而不是让用户对着一堆报错点击。
  onUnauthorized(() => {
    currentUser.value = null
    notify('登录状态已过期，请重新登录')
  })
  // 先确认登录态，避免未登录时发出一堆注定 401 的请求。
  try {
    currentUser.value = await fetchMe()
  } catch (error) {
    // 这里静默是刻意的：fetchMe 已把 401 翻译成 null（未登录属正常情况），
    // 能走到 catch 的是网络或服务端故障。此时退回登录页就是最合理的降级，
    // 再弹一条错误提示只会让「未登录」和「连不上」两种情况长得一模一样。
    console.warn('[auth] 会话恢复失败，按未登录处理', error)
    currentUser.value = null
  }
  authReady.value = true
  if (currentUser.value) await bootstrap()
  marketTimer = window.setInterval(() => { void loadMarketQuotes() }, 60_000)
})

onBeforeUnmount(() => {
  if (marketTimer !== undefined) window.clearInterval(marketTimer)
  window.clearTimeout(searchTimer)
  stopTracePolling()
})
</script>

<template>
  <div v-if="!authReady" class="auth-boot"><RefreshCw :size="18" class="auth-boot-spin" />正在确认登录状态…</div>
  <LoginView v-else-if="!currentUser" @authenticated="handleAuthenticated" />
  <main v-else class="app-shell">
    <aside class="sidebar" :class="{ 'is-open': mobileMenuOpen }">
      <div class="brand-row">
        <div class="brand-mark"><Compass :size="23" stroke-width="1.8" /></div>
        <div>
          <span class="brand-name">基金罗盘</span>
          <span class="brand-subtitle">FUND COMPASS</span>
        </div>
        <button class="icon-button close-mobile" aria-label="关闭菜单" @click="mobileMenuOpen = false"><X :size="18" /></button>
      </div>

      <div class="workspace-label">研究工作台</div>
      <nav class="nav-list" aria-label="主导航">
        <button v-for="item in navigation" :key="item.label" class="nav-item" :class="{ active: activeNav === item.label }" @click="activeNav = item.label; mobileMenuOpen = false">
          <component :is="item.icon" :size="18" stroke-width="1.7" />
          <span>{{ item.label }}</span>
          <span v-if="item.label === '我的观察' && watchlist.length" class="nav-count">{{ watchlist.length }}</span>
        </button>
      </nav>

      <div class="sidebar-spacer"></div>
      <div class="data-pulse">
        <div class="pulse-header"><Activity :size="15" /><span>数据状态</span></div>
        <div class="pulse-main"><span class="pulse-dot" :class="{ warning: marketStatus?.status !== 'ACTIVE' }"></span><span>{{ marketStatusLabel }}</span></div>
        <p>ETF 快照：{{ marketAsOf }}<br />基金数据：{{ dataModeLabel }}</p>
        <button class="subtle-link" @click="activeNav = '实时行情'">查看行情状态 <ArrowUpRight :size="13" /></button>
      </div>
      <div class="user-card">
        <div class="avatar">{{ userInitial }}</div>
        <div><strong>{{ currentUser.displayName }}</strong><span>{{ currentUser.email }}</span></div>
        <button class="icon-button sign-out" title="退出登录" aria-label="退出登录" @click="handleLogout"><LogOut :size="16" /></button>
      </div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <button class="icon-button menu-button" aria-label="打开菜单" @click="mobileMenuOpen = true"><Menu :size="20" /></button>
        <div class="breadcrumb"><span>研究工作台</span><ChevronRight :size="15" /><strong>{{ activeNav }}</strong></div>
        <div class="top-actions">
          <span class="data-chip"><Database :size="14" />{{ dataModeLabel }}</span>
          <button class="icon-button" title="提醒" aria-label="提醒"><BellRing :size="18" /></button>
          <button class="avatar avatar-small" :title="currentUser.email" @click="activeNav = '复盘日志'">{{ userInitial }}</button>
        </div>
      </header>

      <div class="content-scroll">
        <section v-if="activeNav === '基金筛选'" class="research-view">
          <div class="title-row">
            <div>
              <p class="eyebrow">AI 辅助研究 · LLM RESEARCH</p>
              <h1>从目标开始，建立你的候选池</h1>
            </div>
            <button class="trace-button" @click="traceOpen = true"><FileSearch :size="16" />本次研究记录</button>
          </div>

          <section class="query-panel">
            <div class="query-head"><div class="query-label"><Bot :size="18" /><span>描述你的研究目标</span></div><div class="query-engine"><span class="mode-label">{{ aiModeLabel }}</span><button class="ai-config-trigger" title="设置本次会话的大模型" @click="aiSettingsOpen = true"><Settings2 :size="14" />{{ hasSessionLLM ? '本次会话模型' : '设置我的模型' }}</button></div></div>
            <div class="query-input-row">
              <textarea v-model="query" aria-label="研究目标" rows="2"></textarea>
              <button class="run-button" :disabled="isResearchRunning" @click="runResearch"><Sparkles :size="17" />{{ isResearchRunning ? '研究中...' : '开始研究' }}</button>
            </div>
            <div class="understanding-row">
              <span class="understanding-label">已理解</span>
              <button v-for="item in checked" :key="item" class="understanding-chip" @click="checked = checked.filter((tag) => tag !== item)">{{ item }} <X :size="13" /></button>
              <button class="add-condition" @click="checked.push('费用偏好')"><Plus :size="14" />补充条件</button>
            </div>
            <p class="ambiguity"><CircleAlert :size="14" />“波动不要太大”“费用尽量低”仍为偏好项，未被替换成未经确认的数值阈值。</p>
          </section>

          <section v-if="isResearchRunning" class="research-progress" aria-label="研究过程">
            <div class="progress-head">
              <span class="progress-pulse"></span>
              <b>研究进行中</b>
              <span class="progress-hint">Agent 正在调用真实数据工具取证，通常需要 30~90 秒</span>
            </div>
            <ol class="progress-steps">
              <li v-for="(step, index) in liveTrace" :key="index" class="progress-step" :class="step.status.toLowerCase()">
                <span class="step-marker">
                  <Loader2 v-if="step.status === 'RUNNING'" :size="15" class="step-spin" />
                  <Check v-else-if="step.status !== 'FAILED'" :size="14" />
                  <CircleAlert v-else :size="14" />
                </span>
                <span class="step-body"><b>{{ step.title }}</b><small>{{ step.detail }}</small></span>
              </li>
              <li v-if="!liveTrace.length" class="progress-step running">
                <span class="step-marker"><Loader2 :size="15" class="step-spin" /></span>
                <span class="step-body"><b>正在建立候选池…</b><small>正在与数据源同步</small></span>
              </li>
            </ol>
          </section>

          <section v-if="latestRun" class="ai-summary" aria-label="大模型研究结论">
            <div><Sparkles :size="17" /><span>大模型研究结论 · {{ latestRun.modelVersion }}</span></div>
            <p>{{ latestRun.summary }}</p>
            <small v-if="latestRun.knowledgeRefs.length">已引用 {{ latestRun.knowledgeRefs.length }} 个知识库片段；基金事实仍以 AKShare 数据快照为准。</small>
          </section>

          <div class="research-grid">
            <aside class="filter-panel" aria-label="筛选条件">
              <div class="section-caption"><ListFilter :size="16" /><span>筛选条件</span><button @click="resetResearch">重置</button></div>
              <label class="filter-label">基金类型</label>
              <div class="filter-options">
                <button v-for="option in fundTypeOptions" :key="option.label" :class="{ selected: fundType === option.label }" @click="fundType = option.label">{{ option.label }}<em>{{ option.count }}</em></button>
              </div>
              <label class="filter-label">最高风险等级</label>
              <div class="select-wrap"><select v-model="riskLimit"><option>中风险</option><option>中高风险</option><option>高风险</option></select><ChevronDown :size="16" /></div>
              <p v-if="!riskFilterUsable" class="filter-inactive">该条件当前未参与筛选：AKShare 公开排行不提供风险等级，全部候选按“未获取”放行。</p>
              <label class="filter-label">关注主题</label>
              <div class="select-wrap"><select v-model="theme"><option>新能源 / 制造</option><option>科技成长</option><option>均衡配置</option><option>不限主题</option></select><ChevronDown :size="16" /></div>
              <label class="filter-label slider-label">管理费上限 <strong>{{ maxFee.toFixed(2) }}%</strong></label>
              <input v-model.number="maxFee" class="fee-slider" type="range" min="0.4" max="1.5" step="0.1" />
              <div class="scale-row"><span>0.40%</span><span>1.50%</span></div>
              <label class="toggle-row"><span><b>仅看开放申购</b><small>排除暂停与限额状态</small></span><input v-model="requireOpen" type="checkbox" /><i></i></label>
              <div class="filter-foot"><ShieldCheck :size="17" /><p>硬约束由服务端校验，研究结论由已配置的大模型生成。</p></div>
              <p class="filter-caveat">AKShare 公开排行未提供风险等级、申购状态与成立年限，这些条件在数据缺失时不会排除候选，只标注为未核验。</p>
            </aside>

            <section class="results-panel">
              <div class="results-header">
                <div><h2>{{ searchActive ? '搜索结果' : '研究候选' }} <span>{{ displayedFunds.length }}</span></h2><p>{{ searchActive ? `关键词「${searchKeyword.trim()}」命中（全量索引召回）` : '按当前目标与有效数据快照排序' }}</p></div>
                <div class="search-wrap">
                  <Search :size="15" />
                  <input
                    v-model="searchKeyword"
                    class="search-input"
                    type="text"
                    placeholder="按基金名称或 6 位代码搜索，如：白酒 / 161725"
                    aria-label="搜索基金"
                    @input="onSearchInput"
                    @keyup.enter="runSearch"
                    @keydown.esc="clearSearch"
                  />
                  <button v-if="searchKeyword || searchActive" class="icon-button" title="清除搜索" aria-label="清除搜索" @click="clearSearch"><X :size="14" /></button>
                  <span v-if="isSearching" class="search-spinner"></span>
                </div>
              </div>
              <article v-for="(fund, index) in displayedFunds" :key="fund.id" class="fund-card" :class="{ 'fund-warning': fund.status === '注意' }">
                <div class="rank-col"><span class="rank">0{{ index + 1 }}</span><span v-if="index === 0" class="rank-note">优先研究</span></div>
                <div class="fund-main">
                  <div class="fund-title-line"><h3>{{ fund.shortName }}</h3><span class="type-tag">{{ fund.type }}</span><span class="quality-tag" :class="fund.status">{{ fund.status }}</span></div>
                  <p class="fund-name">{{ fund.name }} <span>{{ fund.code }}</span></p>
                  <p class="fund-reason">{{ fund.reason }}</p>
                  <div class="fund-metadata"><span><Clock3 :size="13" />净值 {{ fund.navDate }}</span><span><Landmark :size="13" />{{ fund.company }}</span><span><Check :size="13" />{{ fund.intake }}</span></div>
                </div>
                <div class="fund-stats"><div><span>近 1 年</span><strong :class="{ positive: typeof fund.oneYear === 'number' && fund.oneYear > 0 }">{{ fmtPercent(fund.oneYear) }}</strong></div><div><span>最大回撤</span><strong>{{ fmtPercent(fund.drawdown) }}</strong></div></div>
                <div class="fund-score"><span>参考排序</span><strong>{{ fund.score }}</strong><i>/100</i></div>
                <div class="card-actions"><button class="icon-button" :class="{ active: isWatching(fund) }" :title="isWatching(fund) ? '移出观察' : '加入观察'" @click="toggleWatch(fund)"><Star :size="17" :fill="isWatching(fund) ? 'currentColor' : 'none'" /></button><button class="icon-button" :class="{ active: isComparing(fund) }" :title="isComparing(fund) ? '移出对比' : '加入对比'" @click="toggleCompare(fund)"><ArrowDownUp :size="17" /></button><button class="detail-button" @click="openFund(fund)">查看详情 <ChevronRight :size="15" /></button></div>
              </article>
              <div v-if="!displayedFunds.length" class="empty-module"><Database :size="30" /><h2>暂未得到研究候选</h2><p>{{ emptyHint }}</p></div>
              <p class="results-note"><Info :size="14" />候选不代表买入建议。{{ dataModeDisclaimer }}</p>
            </section>

            <aside class="insight-rail">
              <section v-if="primaryFund" class="compass-card">
                <div class="rail-heading"><span>参考排序说明</span><Info :size="15" /></div>
                <div class="compass-viz" :aria-label="`参考排序 ${primaryFund.score} 分`"><div class="compass-rings"></div><div class="compass-core"><strong>{{ primaryFund.score }}</strong><span>排序值</span></div><span class="compass-point point-1">收益</span><span class="compass-point point-2">费用</span><span class="compass-point point-3">覆盖</span><span class="compass-point point-4">来源</span></div>
                <div v-if="primaryFund.scoreParts.length" class="score-list"><div v-for="part in primaryFund.scoreParts" :key="part.label" class="score-line"><span>{{ part.label }}</span><div class="bar"><i :style="{ width: `${part.value}%`, background: part.color }"></i></div><b>{{ part.value }}</b></div></div>
                <p>排序仅使用当前 AKShare 返回的公开字段，不替代风险评价或投资建议。</p>
              </section>

              <section class="watch-card">
                <div class="rail-heading"><span>我的观察</span><button @click="activeNav = '我的观察'">全部 {{ watchlist.length }} <ChevronRight :size="14" /></button></div>
                <div v-if="watchlist.length" class="watch-items"><button v-for="fund in watchlist.slice(0, 2)" :key="fund.id" class="watch-item" @click="openFund(fund)"><span class="mini-avatar">{{ fund.shortName.slice(0, 1) }}</span><span><b>{{ fund.shortName }}</b><small>{{ fund.reason.slice(0, 18) }}...</small></span><ChevronRight :size="15" /></button></div>
                <div v-else class="empty-watch"><Star :size="18" /><span>还没有观察基金</span></div>
              </section>

              <section class="notice-card"><CircleAlert :size="18" /><div><b>研究边界</b><p>不执行交易，不预测收益。请结合个人情况独立判断。</p></div></section>
            </aside>
          </div>
        </section>

        <section v-else-if="activeNav === '实时行情'" class="module-view">
          <div class="title-row">
            <div><p class="eyebrow">INTRADAY MARKET DATA</p><h1>ETF / LOF 场内行情</h1></div>
            <button class="icon-button refresh-market-button" :class="{ spinning: isMarketRefreshing }" :disabled="!apiConnected || isMarketRefreshing" title="刷新行情" aria-label="刷新行情" @click="refreshLiveMarket"><RefreshCw :size="18" /></button>
          </div>
          <section class="market-status-bar" :class="marketStatus?.status?.toLowerCase() ?? 'pending'">
            <Radio :size="17" /><div><b>{{ marketStatusLabel }}</b><span>{{ marketStatus?.sourceName ?? '等待 API 连接' }} · 最近成功快照 {{ marketAsOf }}</span></div><small>{{ marketStatus?.refreshInterval ? `刷新间隔 ${marketStatus.refreshInterval}` : '' }}</small>
          </section>
          <section class="market-quote-panel">
            <header><div><h2>场内价格</h2><p>仅展示已拉取的 ETF 行情字段</p></div><span class="type-tag">{{ marketQuotes.length }} 只</span></header>
            <div v-if="marketQuotes.length" class="market-quote-table" role="table" aria-label="ETF 场内行情">
              <div class="market-quote-row market-quote-head" role="row"><span>标的</span><span>场内价格</span><span>涨跌幅</span><span>成交额</span><span>换手率</span><span>行情时间</span></div>
              <div v-for="quote in marketQuotes" :key="quote.code" class="market-quote-row" role="row">
                <span class="quote-name"><b>{{ quote.name }}</b><small>{{ quote.venue }} · {{ quote.code }}</small></span>
                <strong>{{ fmtPrice(quote.marketPrice) }}</strong>
                <strong :class="{ positive: quote.changePercent > 0, negative: quote.changePercent < 0 }">{{ fmtPercent(quote.changePercent) }}</strong>
                <span>{{ fmtAmount(quote.amountWan) }}</span>
                <span>{{ quote.turnoverPercent.toFixed(2) }}%</span>
                <span class="quote-time"><b :class="{ 'freshness-stale': quote.freshnessStatus === 'STALE' }">{{ quote.freshnessStatus === 'ACTIVE' ? '有效' : '过期' }}</b><small>{{ formatTime(quote.asOf) }}</small><em v-if="quote.staleReason">{{ quote.staleReason }}</em></span>
              </div>
            </div>
            <div v-else class="market-empty"><LineChart :size="30" /><h2>尚未得到行情快照</h2><p>行情源状态会保留最近成功结果；本页不会用估算数据填充空值。</p></div>
          </section>
          <section class="market-disclaimer"><CircleAlert :size="17" /><p>{{ marketDisclaimer }}</p></section>
          <section class="market-boundary"><div><span>正式净值</span><b>开放式基金按日频正式披露</b></div><div><span>实时字段</span><b>ETF/LOF 场内价格与成交数据</b></div><div><span>推荐边界</span><b>场内行情不参与当前候选排序</b></div></section>
        </section>

        <section v-else-if="activeNav === '对比台'" class="module-view">
          <div class="title-row"><div><p class="eyebrow">同口径比较</p><h1>把差异放在同一把尺子上</h1></div><button class="trace-button" @click="activeNav = '基金筛选'"><Search :size="16" />继续添加基金</button></div>
          <div class="method-bar"><ShieldCheck :size="17" /><span>指标窗口：近 1 年 · 数据模式：{{ dataModeLabel }} · 净值按日频披露</span></div>
          <div v-if="compareList.length" class="comparison-table"><div class="compare-row compare-head"><span>基金</span><div v-for="fund in compareList" :key="fund.id"><b>{{ fund.shortName }}</b><small>{{ fund.code }}</small><button @click="toggleCompare(fund)"><X :size="14" /></button></div></div><div v-for="metric in [{ label: '参考排序', key: 'score', suffix: '' }, { label: '近 1 年', key: 'oneYear', suffix: '%' }, { label: '年化波动', key: 'volatility', suffix: '%' }, { label: '最大回撤', key: 'drawdown', suffix: '%' }, { label: '管理费率', key: 'fee', suffix: '%' }, { label: '基金规模', key: 'scale', suffix: ' 亿元' } ]" :key="metric.key" class="compare-row"><span>{{ metric.label }}</span><div v-for="fund in compareList" :key="fund.id" :class="metric.key === 'oneYear' ? metricTone(fund[metric.key as keyof Fund]) : ''">{{ fmtFundMetric(fund[metric.key as keyof Fund], metric.key === 'score' || metric.key === 'scale' ? 0 : 2) }}{{ typeof fund[metric.key as keyof Fund] === 'number' ? metric.suffix : '' }}</div></div><div class="compare-row"><span>研究提示</span><div v-for="fund in compareList" :key="fund.id" class="compare-note">{{ fund.caveat }}</div></div></div>
          <div v-else class="empty-module"><ArrowDownUp :size="30" /><h2>把候选加入对比台</h2><p>从基金筛选页选择最多四只基金，以同一数据口径横向查看。</p><button class="run-button" @click="activeNav = '基金筛选'">返回筛选</button></div>
        </section>

        <section v-else-if="activeNav === '我的观察'" class="module-view">
          <div class="title-row"><div><p class="eyebrow">持续观察</p><h1>为下一次判断留下依据</h1></div><button class="trace-button" @click="activeNav = '基金筛选'"><Plus :size="16" />添加基金</button></div>
          <div class="watchlist-module"><article v-for="fund in watchlist" :key="fund.id" class="watchlist-card"><div class="watchlist-card-head"><div><span class="mini-avatar large">{{ fund.shortName.slice(0, 1) }}</span><div><h2>{{ fund.shortName }}</h2><p>{{ fund.name }} · {{ fund.code }}</p></div></div><button class="icon-button" title="移出观察" @click="toggleWatch(fund)"><X :size="17" /></button></div><div class="watch-metrics"><span>罗盘适配 <b>{{ fund.score }}</b></span><span>净值日期 <b>{{ fund.navDate }}</b></span><span>数据状态 <b>{{ fund.status }}</b></span></div><div class="watch-note"><label>加入原因与下一次复核</label><textarea v-model="noteDraft" placeholder="例如：与新能源主题候选比较后，再决定是否继续关注。"></textarea><button class="small-save" @click="addNote">保存备注</button></div></article><div v-if="!watchlist.length" class="empty-module"><Star :size="30" /><h2>观察列表还是空的</h2><p>先把值得继续研究的基金加入这里，记录当时的理由。</p><button class="run-button" @click="activeNav = '基金筛选'">开始筛选</button></div></div>
        </section>

        <section v-else-if="activeNav === '复盘日志'" class="module-view">
          <div class="title-row">
            <div><p class="eyebrow">研究存档 · CONVERSATIONS</p><h1>每一轮研究都留在这里</h1></div>
            <button class="trace-button" @click="startNewConversation"><Plus :size="16" />开始新一轮研究</button>
          </div>
          <div v-if="conversations.length" class="conversation-grid">
            <article v-for="item in conversations" :key="item.id" class="conversation-card" :class="{ active: activeConversationId === item.id }">
              <button class="conversation-main" @click="openConversation(item.id)">
                <h3>{{ item.title }}</h3>
                <p>{{ item.messageCount }} 条消息 · {{ formatTime(item.updatedAt ?? '') }}</p>
              </button>
              <button class="icon-button" title="删除这条记录" aria-label="删除这条记录" @click.stop="removeConversation(item.id)"><X :size="15" /></button>
            </article>
          </div>
          <div v-else class="empty-module">
            <CalendarClock :size="30" />
            <h2>还没有研究记录</h2>
            <p>每次「开始研究」都会在这里留下一条记录，包含当时的研究目标和模型结论，重开服务也不会丢。</p>
            <button class="run-button" @click="activeNav = '基金筛选'">去研究</button>
          </div>

          <section v-if="conversationLoading" class="conversation-thread"><p class="thread-loading">正在读取…</p></section>
          <section v-else-if="conversationMessages.length" class="conversation-thread">
            <div v-for="message in conversationMessages" :key="message.id" class="thread-message" :class="message.role">
              <span class="thread-role">{{ message.role === 'user' ? '研究目标' : '模型结论' }}</span>
              <p>{{ message.content }}</p>
              <small>{{ formatTime(message.createdAt ?? '') }}</small>
            </div>
          </section>
        </section>

        <section v-else class="module-view placeholder-module">
          <div class="title-row"><div><p class="eyebrow">功能规划</p><h1>{{ activeNav }}</h1></div></div>
          <div class="empty-module"><SlidersHorizontal :size="30" /><h2>此模块尚未接入真实数据工作流</h2><p>当前版本只展示由 AKShare 提供的公开参考基金数据；风险画像将在完成持久化后开放。</p><button class="run-button" @click="activeNav = '基金筛选'">回到研究工作台</button></div>
        </section>
      </div>
    </section>

    <div v-if="selectedFund" class="overlay" @click.self="selectedFund = null">
      <aside class="detail-drawer">
        <header><div><p class="eyebrow">基金详情 · {{ selectedHistory?.mode ?? 'REFERENCE' }}</p><h2>{{ selectedFund.shortName }}</h2><p>{{ selectedFund.name }} · {{ selectedFund.code }}</p></div><button class="icon-button" aria-label="关闭详情" @click="selectedFund = null"><X :size="19" /></button></header>
        <div class="drawer-score-row"><div class="drawer-score"><span>参考排序</span><strong>{{ selectedFund.score }}</strong><small>/100</small></div><div><span class="quality-tag" :class="selectedFund.status">{{ selectedFund.status }}</span><p>{{ selectedFund.reason }}</p></div></div>
        <div class="chart-panel"><div class="chart-label"><span>单位净值走势</span><small>{{ historyLoading ? '正在读取历史净值...' : selectedHistory?.items.length ? `AKShare · ${selectedHistory.items.length} 个交易日` : 'AKShare 未返回历史净值' }}</small></div><svg v-if="selectedHistory?.items.length" viewBox="0 0 300 104" role="img" aria-label="单位净值走势"><defs><linearGradient id="chartFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#1d6b68" stop-opacity=".22"/><stop offset="100%" stop-color="#1d6b68" stop-opacity="0"/></linearGradient></defs><path :d="chartArea(selectedFund)" fill="url(#chartFill)"/><path :d="chartLine(selectedFund)" fill="none" stroke="#1d6b68" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
        <div class="metric-grid"><div><span>单位净值</span><b>{{ fmtNumber(selectedFund.nav, 4) }}</b><small>{{ selectedFund.navDate || '未获取' }}</small></div><div><span>区间收益</span><b class="positive">{{ fmtPercent(selectedHistory?.metrics?.totalReturn as number | undefined) }}</b><small>{{ selectedHistory?.period ?? '历史数据未获取' }}</small></div><div><span>年化波动</span><b>{{ fmtPercent(selectedHistory?.metrics?.volatility as number | undefined) }}</b><small>{{ selectedHistory?.metricVersion ?? '未计算' }}</small></div><div><span>最大回撤</span><b>{{ fmtPercent(selectedHistory?.metrics?.drawdown as number | undefined) }}</b><small>历史窗口</small></div><div><span>管理费率</span><b>{{ fmtPercent(selectedFund.fee) }}</b><small>AKShare 排行字段</small></div><div><span>数据质量</span><b>{{ selectedHistory?.metricStatus ?? selectedFund.qualityStatus ?? selectedFund.status }}</b><small>{{ selectedHistory?.sourceType ?? selectedFund.navSourceType ?? 'AKSHARE_PUBLIC' }} · {{ selectedHistory?.trustLevel ?? selectedFund.navTrustLevel ?? 'LOW' }}</small></div></div>
        <section class="drawer-section"><h3>研究证据</h3><div class="evidence-list"><div><Check :size="15" /><span>净值与指标：{{ selectedFund.navDate }} · 已通过数据质量检查</span></div><div><Check :size="15" /><span>来源：{{ selectedFund.source }}</span></div><div><CircleAlert :size="15" /><span>{{ selectedFund.caveat }}</span></div></div></section>
        <section class="drawer-section"><h3>数据口径</h3><p class="source-copy">数据快照 {{ selectedFund.snapshot }}。来源 {{ selectedHistory?.sourceType ?? selectedFund.navSourceType ?? 'AKSHARE_PUBLIC' }}，可信度 {{ selectedHistory?.trustLevel ?? selectedFund.navTrustLevel ?? 'LOW' }}。普通开放式基金仅展示正式披露净值，不将场内行情或第三方估算混为最新净值。</p></section>
        <footer><button class="secondary-action" @click="toggleCompare(selectedFund!)"><ArrowDownUp :size="16" />{{ isComparing(selectedFund) ? '移出对比' : '加入对比' }}</button><button class="run-button" @click="toggleWatch(selectedFund!)"><Star :size="16" :fill="isWatching(selectedFund) ? 'currentColor' : 'none'" />{{ isWatching(selectedFund) ? '已在观察' : '加入观察' }}</button></footer>
      </aside>
    </div>

    <div v-if="traceOpen" class="overlay trace-overlay" @click.self="traceOpen = false"><section class="trace-modal"><header><div><p class="eyebrow">RECOMMENDATION TRACE</p><h2>本次研究记录</h2><p>{{ latestRun?.runId ?? '尚未运行研究' }} · {{ latestRun?.mode ?? 'LLM_RESEARCH' }}</p></div><button class="icon-button" aria-label="关闭研究记录" @click="traceOpen = false"><X :size="19" /></button></header><div class="trace-timeline"><div v-for="(step, index) in activeTrace" :key="step.title" class="trace-step"><span>{{ index + 1 }}</span><div><b>{{ step.title }}</b><p>{{ step.detail }}</p></div><Check :size="17" /></div></div><div class="trace-data"><div><span>模型版本</span><b>{{ latestRun?.modelVersion ?? aiModeLabel }}</b></div><div><span>模型状态</span><b>{{ latestRun?.interpretation.provider ?? aiModeLabel }}</b></div><div><span>数据模式</span><b>{{ dataModeLabel }}</b></div><div><span>结果校验</span><b class="ok">候选代码与数据快照已校验</b></div></div><div class="trace-warning"><CircleAlert :size="17" /><p>模型根据用户目标、真实候选数据与可引用知识片段生成研究结论；基金事实、数据来源和硬性边界由服务端校验，结果不构成交易指令。</p></div></section></div>
    <div v-if="aiSettingsOpen" class="overlay ai-config-overlay" @click.self="aiSettingsOpen = false"><section class="ai-config-modal" aria-label="本次会话模型设置"><header><div><p class="eyebrow">SESSION-ONLY BYOK</p><h2>设置我的大模型</h2><p>API Key 只保存在当前页面内存中，仅随研究请求发送；不会保存到服务器、浏览器存储或研究记录。</p></div><button class="icon-button" aria-label="关闭模型设置" @click="aiSettingsOpen = false"><X :size="19" /></button></header><div class="ai-engine-state"><span class="pulse-dot"></span><div><b>{{ hasSessionLLM ? `${sessionLLM.provider} · ${sessionLLM.model}` : '尚未设置会话模型' }}</b><small>{{ hasSessionLLM ? '本次会话研究将使用你的 API Key' : '未设置时使用服务端开发配置（若已配置）' }}</small></div></div><div class="ai-config-form"><label>Provider<select v-model="sessionLLM.provider"><option value="openai-compatible">OpenAI-compatible</option><option value="ollama">Ollama</option></select></label><label>Base URL<input v-model.trim="sessionLLM.baseUrl" type="url" autocomplete="off" placeholder="https://api.openai.com/v1" /></label><label>Model<input v-model.trim="sessionLLM.model" type="text" autocomplete="off" placeholder="gpt-4o-mini" /></label><label>API Key<input v-model="sessionLLM.apiKey" type="password" autocomplete="off" placeholder="仅本次会话使用" /></label><p class="ai-config-hint"><ShieldCheck :size="15" />离开或刷新页面后，当前 Key 会从浏览器内存中消失。公网模型地址必须使用 HTTPS，并且需要通过服务端域名白名单。</p><footer><button class="secondary-action" @click="clearSessionLLM">清除 Key</button><button class="run-button" @click="aiSettingsOpen = false">完成</button></footer></div></section></div>
    <div v-if="toast" class="toast"><Check :size="17" />{{ toast }}</div>
  </main>
</template>
