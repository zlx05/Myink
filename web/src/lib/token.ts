// JWT 存取（localStorage 单源）+ 401 事件（解耦 context↔api 循环依赖）。
// 业务请求带 Bearer；网关验签后透传 X-Myink-User 给 Python 做归属断言（§14.1 ③）。

const TOKEN_KEY = 'myink.token'
const USER_ID_KEY = 'myink.user_id'
const USERNAME_KEY = 'myink.username'

/** 401 登出事件名：api 层 dispatch，AuthContext 订阅 */
export const UNAUTHORIZED_EVENT = 'myink:unauthorized'

export interface StoredSession {
  token: string
  userId: string
  username: string
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getSession(): StoredSession | null {
  const token = getToken()
  const userId = localStorage.getItem(USER_ID_KEY)
  const username = localStorage.getItem(USERNAME_KEY)
  if (!token || !userId || !username) return null
  return { token, userId, username }
}

export function setSession(session: StoredSession): void {
  localStorage.setItem(TOKEN_KEY, session.token)
  localStorage.setItem(USER_ID_KEY, session.userId)
  localStorage.setItem(USERNAME_KEY, session.username)
}

export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_ID_KEY)
  localStorage.removeItem(USERNAME_KEY)
}

/** 由 api 层在收到 401 时派发，AuthContext 统一登出（避免每个调用点重复处理） */
export function dispatchUnauthorized(): void {
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
}
