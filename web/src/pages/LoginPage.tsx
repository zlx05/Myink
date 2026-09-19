import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { ApiError } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import { isValidNewPassword, NEW_PASSWORD_VALIDATION_MESSAGE } from '../lib/passwordPolicy'
import styles from './LoginPage.module.css'

type Mode = 'login' | 'register'

const USERNAME_RE = /^[A-Za-z0-9_-]{3,64}$/

function passwordLength(value: string): number {
  return Array.from(value).length
}

export default function LoginPage() {
  const { login, register, notice } = useAuth()
  const navigate = useNavigate()
  const [mode, setMode] = useState<Mode>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [invitationCode, setInvitationCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function switchMode(next: Mode) {
    setMode(next)
    setPassword('')
    setConfirmPassword('')
    setInvitationCode('')
    setError(null)
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    const rawName = username.trim()
    if (!USERNAME_RE.test(rawName)) {
      setError('用户名需为 3–64 位英文字母、数字、下划线或短横线')
      return
    }
    const name = rawName.toLowerCase()
    const length = passwordLength(password)
    if (mode === 'login' && (length < 1 || length > 128)) {
      setError('请输入有效密码（最多 128 个字符）')
      return
    }
    if (mode === 'register' && !isValidNewPassword(password)) {
      setError(NEW_PASSWORD_VALIDATION_MESSAGE)
      return
    }
    if (mode === 'register' && password !== confirmPassword) {
      setError('两次输入的密码不一致')
      return
    }
    const invitation = invitationCode.trim()
    if (mode === 'register' && !invitation) {
      setError('请输入邀请码')
      return
    }

    setBusy(true)
    setError(null)
    try {
      const accepted = mode === 'login'
        ? await login(name, password)
        : await register(name, password, invitation)
      if (accepted) navigate('/projects', { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.code === 'network_error') {
        setError('无法连接服务，请确认网关（:8080）已启动')
      } else {
        setError(formatApiError(err, mode === 'login' ? '登录失败' : '注册失败'))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.wrap}>
      <form className={`panel ${styles.card}`} onSubmit={onSubmit}>
        <div>
          <h1>Myink 作品工作台</h1>
          <p className={styles.sub}>长篇网文多 Agent 创作助手</p>
        </div>
        <div className={styles.tabs} role="tablist" aria-label="账号入口">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'login'}
            className={mode === 'login' ? styles.tabActive : styles.tab}
            onClick={() => switchMode('login')}
          >
            登录
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'register'}
            className={mode === 'register' ? styles.tabActive : styles.tab}
            onClick={() => switchMode('register')}
          >
            注册账号
          </button>
        </div>
        <label className={styles.field}>
          <span>用户名</span>
          <input
            className="input"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus
          />
        </label>
        <label className={styles.field}>
          <span>密码</span>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          />
        </label>
        {mode === 'register' && (
          <>
            <p className={styles.sub}>
              Administrators can see writing and debugging content（管理员可查看写作与调试内容）。
            </p>
            <label className={styles.field}>
              <span>确认密码</span>
              <input
                className="input"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
              />
            </label>
            <label className={styles.field}>
              <span>邀请码</span>
              <input
                className="input"
                value={invitationCode}
                onChange={(event) => setInvitationCode(event.target.value)}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
              />
            </label>
          </>
        )}
        {error && <div className="banner banner-error" role="alert">{error}</div>}
        {!error && notice && <div className="banner banner-warning" role="status">{notice}</div>}
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? (mode === 'login' ? '登录中…' : '注册中…') : (mode === 'login' ? '登录' : '创建账号')}
        </button>
      </form>
    </div>
  )
}
