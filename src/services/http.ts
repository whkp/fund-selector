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

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  }
  if (authToken) headers.Authorization = `Bearer ${authToken}`

  const response = await fetch(`${apiBase}${path}`, { ...init, headers })
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
  return response.json() as Promise<T>
}
