import { ApiError, request, setToken } from './http'

export type AuthUser = {
  id: string
  email: string
  displayName: string
  role: string
  createdAt: string | null
  lastLoginAt: string | null
}

/** 公开的注册准入策略。刻意不含邀请码本身，这个接口不需要登录即可访问。 */
export type AuthPolicy = {
  inviteRequired: boolean
  inviteCodeCount: number
}

export type Conversation = {
  id: string
  title: string
  status: string
  messageCount: number
  createdAt: string | null
  updatedAt: string | null
  lastMessageAt: string | null
}

export type ConversationMessage = {
  id: string
  role: 'user' | 'assistant' | string
  content: string
  runId: string | null
  payload: Record<string, unknown> | null
  createdAt: string | null
}

type SessionResponse = { token: string; expiresAt: number; tokenType: string; user: AuthUser }

export async function login(email: string, password: string): Promise<AuthUser> {
  const session = await request<SessionResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  setToken(session.token)
  return session.user
}

export async function register(
  email: string,
  password: string,
  displayName = '',
  inviteCode = '',
): Promise<AuthUser> {
  const session = await request<SessionResponse>('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ email, password, displayName, inviteCode }),
  })
  setToken(session.token)
  return session.user
}

/** 拉取注册准入策略，让登录页自己决定要不要显示邀请码输入框。 */
export async function fetchAuthPolicy(): Promise<AuthPolicy> {
  return request<AuthPolicy>('/auth/policy')
}

/** 用已保存的令牌换取当前用户。令牌无效时返回 null，由调用方决定是否跳登录页。 */
export async function fetchMe(): Promise<AuthUser | null> {
  try {
    const payload = await request<{ user: AuthUser }>('/auth/me')
    return payload.user
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null
    throw error
  }
}

export async function logout(): Promise<void> {
  try {
    await request<void>('/auth/logout', { method: 'POST' })
  } catch {
    // 服务端不可达也要让本地登出成功，否则用户会被卡在登录态里。
  } finally {
    setToken(null)
  }
}

export async function fetchConversations(): Promise<Conversation[]> {
  const payload = await request<{ items: Conversation[] }>('/conversations')
  return payload.items
}

export async function fetchConversation(id: string): Promise<{ conversation: Conversation; messages: ConversationMessage[] }> {
  return request<{ conversation: Conversation; messages: ConversationMessage[] }>(`/conversations/${id}`)
}

export async function createConversation(title = ''): Promise<Conversation> {
  return request<Conversation>('/conversations', {
    method: 'POST',
    body: JSON.stringify({ title }),
  })
}

export async function deleteConversation(id: string): Promise<void> {
  await request<void>(`/conversations/${id}`, { method: 'DELETE' })
}
