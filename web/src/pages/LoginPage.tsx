// 登录：POST /api/v1/auth/token（username → JWT，MVP 用户名即身份）。成功后跳项目库。
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { ApiError } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import styles from './LoginPage.module.css'

export default function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('demo')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    const name = username.trim()
    if (!name) return
    setBusy(true)
    setError(null)
    try {
      await login(name)
      navigate('/projects', { replace: true })
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.code === 'network_error'
            ? '无法连接服务，请确认网关（:8080）已启动'
            : formatApiError(err, '登录失败'),
        )
      } else {
        setError('登录失败')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.wrap}>
      <form className={`panel ${styles.card}`} onSubmit={onSubmit}>
        <h1>Myink 作品工作台</h1>
        <p className={styles.sub}>长篇网文多 Agent 创作助手</p>
        <p className={styles.sub}>本地演示模式：使用 demo 进入，无密码验证。</p>
        <label className={styles.field}>
          <span>用户名</span>
          <input
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
        </label>
        {error && <div className="banner banner-error">{error}</div>}
        <button className="btn btn-primary" type="submit" disabled={busy || !username.trim()}>
          {busy ? '登录中…' : '进入工作台'}
        </button>
      </form>
    </div>
  )
}
