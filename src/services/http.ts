// 统一的 HTTP 客户端：拼地址、带令牌、把后端的错误信封翻译成异常。
//
// `||` 而不是 `??`：未设置 VITE_API_BASE 时它是空字符串而不是 null/undefined，
// 用 `??` 会让空串通过，所有请求都打到静态托管上。
const apiBase = import.meta.env.VITE_API_BASE || '/api'

const TOKEN_KEY = 'fund-compass.token'

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    // 隐私模式 / 禁用存储时退化为「仅内存」，刷新后需要重新登录。
    return null
  }
}

let authToken: string | null = readStoredToken()
let unauthorizedHandler: (() => void) | null = null

export class ApiError extends Error {
  status: number
  code: string

  constructor(message: string, status: number, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

export function getToken(): string | null {
  return authToken
}

export function setToken(value: string | null): void {
  authToken = value
  try {
    if (value) window.localStorage.setItem(TOKEN_KEY, value)
    else window.localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 存储不可用时只保留内存副本 */
  }
}

/** 令牌失效时触发，让上层把界面切回登录页。 */
export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

/**
 * 请求超时（毫秒）。
 *
 * 不给超时的话，服务端卡住或网络中途断掉时 fetch 会一直挂着（浏览器默认
 * 可以到几分钟），界面就永远停在转圈状态，用户既等不到结果也没法取消。
 */
export const TIMEOUT_MS = {
  /** 普通读写：登录、对话增删改、自选。 */
  default: 20_000,
  /** 会触发服务端去上游抓取的读取：基金列表、历史净值、行情。 */
  upstream: 60_000,
  /**
   * 研究任务：服务端给 LLM 的超时是 180 秒，实测一次完整请求约 48 秒。
   * 这里留出余量，免得客户端比服务端先放弃、把已经跑完的结果丢掉。
   */
  research: 200_000,
} as const

export type RequestOptions = {
  /** 覆盖默认超时，见 `TIMEOUT_MS`。 */
  timeoutMs?: number
}

/**
 * 把「调用方自己的 signal」和「超时」合成一个取消信号。
 *
 * 没用 `AbortSignal.timeout` / `AbortSignal.any`：前者不给句柄，定时器没法清掉；
 * 后者要求较新的浏览器。更重要的是这里需要把"超时"和"调用方主动取消"区分开 ——
 * 前者要提示用户，后者是正常流程（比如组件卸载），不该弹错误。手工建 controller
 * 才能把这个状态记下来。
 */
function withTimeout(ms: number, external?: AbortSignal | null) {
  const controller = new AbortController()
  let timedOut = false
  const timer = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, ms)
  const forward = () => controller.abort()
  if (external) {
    if (external.aborted) controller.abort()
    else external.addEventListener('abort', forward, { once: true })
  }
  return {
    signal: controller.signal,
    timedOut: () => timedOut,
    dispose: () => {
      window.clearTimeout(timer)
      external?.removeEventListener('abort', forward)
    },
  }
}

type ErrorPayload = {
  error?: { code?: string; message?: string }
  detail?: { code?: string; message?: string } | string
}

function describeError(payload: ErrorPayload | null, status: number): { message: string; code: string } {
  const detail = payload?.detail
  const message = payload?.error?.message
    ?? (typeof detail === 'object' ? detail?.message : detail)
    ?? `请求失败（${status}）`
  const code = payload?.error?.code ?? (typeof detail === 'object' ? detail?.code ?? '' : '')
  return { message, code }
}

export async function request<T>(
  path: string,
  init?: RequestInit,
  options?: RequestOptions,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  }
  if (authToken) headers.Authorization = `Bearer ${authToken}`

  const timeoutMs = options?.timeoutMs ?? TIMEOUT_MS.default
  const guard = withTimeout(timeoutMs, init?.signal)
  try {
    const response = await fetch(`${apiBase}${path}`, { ...init, headers, signal: guard.signal })
    if (response.status === 401) {
      // 只有「本来带着令牌却被打回来」才算会话失效；
      // 登录接口自己的 401 不应该弹出「登录过期」。
      const hadToken = Boolean(authToken)
      setToken(null)
      if (hadToken) unauthorizedHandler?.()
    }
    if (!response.ok) {
      const payload = (await response.json().catch(() => null)) as ErrorPayload | null
      const { message, code } = describeError(payload, response.status)
      throw new ApiError(message, response.status, code)
    }
    if (response.status === 204) return undefined as T
    return (await response.json()) as T
  } catch (error) {
    // 上面按错误信封造的 ApiError 原样放行，这里只翻译网络层的失败。
    if (error instanceof ApiError) throw error
    if (guard.timedOut()) {
      const seconds = Math.round(timeoutMs / 1000)
      throw new ApiError(`请求超时（${seconds} 秒无响应），请重试`, 0, 'TIMEOUT')
    }
    // 调用方主动取消（如组件卸载）属于正常流程，不该被包装成错误提示。
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    if (error instanceof SyntaxError) {
      throw new ApiError('服务端返回了无法解析的内容，请稍后重试', 0, 'BAD_RESPONSE')
    }
    throw new ApiError('网络连接失败，请检查网络后重试', 0, 'NETWORK')
  } finally {
    guard.dispose()
  }
}
