import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { ProjectRail } from '../components/ProjectRail'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import { isValidNewPassword, NEW_PASSWORD_VALIDATION_MESSAGE } from '../lib/passwordPolicy'
import type { Project } from '../types'
import styles from './AccountPage.module.css'

export default function AccountPage() {
  const { session, status, changePassword, logout } = useAuth()
  const [projects, setProjects] = useState<Project[]>([])
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    api.listProjects()
      .then((items) => { if (alive) setProjects(items) })
      .catch(() => { if (alive) setProjects([]) })
    return () => { alive = false }
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!isValidNewPassword(newPassword)) {
      setError(`新${NEW_PASSWORD_VALIDATION_MESSAGE}`)
      return
    }
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致')
      return
    }
    if (!currentPassword) {
      setError('请输入当前密码')
      return
    }

    setBusy(true)
    setError(null)
    try {
      await changePassword(currentPassword, newPassword)
      // 后端已使该账号全部 token 失效，AuthContext 会清除本机会话并回到登录页。
    } catch (err) {
      setError(formatApiError(err, '密码修改失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.wrap}>
      <ProjectRail projects={projects} onLogout={logout} />
      <main className={styles.main}>
        <header className={styles.header}>
          <h1>账号</h1>
          <p>查看当前账号并更新登录密码。</p>
        </header>

        <section className={`panel ${styles.section}`} aria-labelledby="current-account">
          <h2 id="current-account">当前账号</h2>
          <dl className={styles.account}>
            <div>
              <dt>用户名</dt>
              <dd>{session?.username}</dd>
            </div>
            <div>
              <dt>账号级别</dt>
              <dd>{session?.tier ?? 'normal'}</dd>
            </div>
            <div>
              <dt>账号角色</dt>
              <dd>{session?.role ?? 'user'}</dd>
            </div>
          </dl>
        </section>

        {status === 'authenticated' && session?.role === 'admin' && session.roleVerified === true && (
          <section className={`panel ${styles.section}`} aria-labelledby="admin-console">
            <h2 id="admin-console">管理后台</h2>
            <p className={styles.hint}>查看全局用户、作品、任务、运行与访问日志；后台仅提供只读观测。</p>
            <Link className="btn btn-secondary" to="/admin">打开管理后台</Link>
          </section>
        )}

        <section className={`panel ${styles.section}`} aria-labelledby="change-password">
          <h2 id="change-password">修改密码</h2>
          <p className={styles.hint}>修改成功后，该账号在所有浏览器中的登录都会失效，需要重新登录。</p>
          <form className={styles.form} onSubmit={submit}>
            <label className={styles.field}>
              <span>当前密码</span>
              <input
                className="input"
                type="password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                autoComplete="current-password"
              />
            </label>
            <label className={styles.field}>
              <span>新密码</span>
              <input
                className="input"
                type="password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                autoComplete="new-password"
              />
            </label>
            <label className={styles.field}>
              <span>确认新密码</span>
              <input
                className="input"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
              />
            </label>
            {error && <div className="banner banner-error" role="alert">{error}</div>}
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? '修改中…' : '修改密码'}
            </button>
          </form>
        </section>

        <section className={`panel ${styles.section}`} aria-labelledby="password-help">
          <h2 id="password-help">忘记密码</h2>
          <p className={styles.hint}>
            本地部署不提供邮件找回。请联系本机管理员，通过 Myink 管理命令为账号重设密码。
          </p>
        </section>
      </main>
    </div>
  )
}
