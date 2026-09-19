// JWT 存取（localStorage 单源）+ 401 事件（解耦 context↔api 循环依赖）。
// 业务请求带 Bearer；网关验签后透传 X-Myink-User 给 Python 做归属断言（§14.1 ③）。

export const SESSION_STORAGE_KEY = 'myink.session'
const LEGACY_TOKEN_KEY = 'myink.token'
const LEGACY_USER_ID_KEY = 'myink.user_id'
const LEGACY_USERNAME_KEY = 'myink.username'

/** 401 登出事件名：api 层 dispatch，AuthContext 订阅 */
export const UNAUTHORIZED_EVENT = 'myink:unauthorized'

export interface StoredSession {
  token: string
  userId: string
  username: string
  tier: string
  role: 'user' | 'admin'
  roleVerified: boolean
  expiresAt: number | null
}

export function getToken(): string | null {
  return getSession()?.token ?? null
}

export function getSession(): StoredSession | null {
  const raw = localStorage.getItem(SESSION_STORAGE_KEY)
  if (raw) {
    try {
      const value = JSON.parse(raw) as Partial<StoredSession>
      if (!value.token || !value.userId || !value.username || !value.tier) return null
      const expiresAt = typeof value.expiresAt === 'number' ? value.expiresAt : null
      if (expiresAt !== null && expiresAt <= Date.now()) {
        clearSession()
        return null
      }
      return {
        token: value.token,
        userId: value.userId,
        username: value.username,
        tier: value.tier,
        role: value.role === 'admin' ? 'admin' : 'user',
        roleVerified: false,
        expiresAt,
      }
    } catch {
      clearSession()
      return null
    }
  }

  // 一次性兼容升级前的三个认证键；服务器校验成功后会写成原子 JSON。
  const token = localStorage.getItem(LEGACY_TOKEN_KEY)
  const userId = localStorage.getItem(LEGACY_USER_ID_KEY)
  const username = localStorage.getItem(LEGACY_USERNAME_KEY)
  if (!token || !userId || !username) return null
  return {
    token, userId, username, tier: 'normal', role: 'user', roleVerified: false, expiresAt: null,
  }
}

export function setSession(session: StoredSession): void {
  localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))
  localStorage.removeItem(LEGACY_TOKEN_KEY)
  localStorage.removeItem(LEGACY_USER_ID_KEY)
  localStorage.removeItem(LEGACY_USERNAME_KEY)
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_STORAGE_KEY)
  localStorage.removeItem(LEGACY_TOKEN_KEY)
  localStorage.removeItem(LEGACY_USER_ID_KEY)
  localStorage.removeItem(LEGACY_USERNAME_KEY)
}

/** 由 api 层在收到 401 时派发，AuthContext 统一登出（避免每个调用点重复处理） */
export function dispatchUnauthorized(token: string | null = getToken()): void {
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: { token } }))
}
