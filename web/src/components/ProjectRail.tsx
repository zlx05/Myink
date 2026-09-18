// 左 rail：作品列表；进书后是设定/创作设置/全局审计；作品库等全局页才露出环境配置和主题。
import { NavLink, useParams } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import type { Project } from '../types'
import styles from './ProjectRail.module.css'

interface Props {
  projects: Project[]
  onLogout: () => void
}

export function ProjectRail({ projects, onLogout }: Props) {
  const { session } = useAuth()
  const { projectId } = useParams()
  return (
    <aside className={styles.rail}>
      <NavLink to="/projects" className={styles.brand}>
        Myink
      </NavLink>
      <nav className={styles.nav} aria-label="作品列表">
        {projects.map((p) => (
          <NavLink
            key={p.id}
            to={`/projects/${p.id}`}
            className={({ isActive }) => (isActive ? `${styles.item} ${styles.active}` : styles.item)}
          >
            <span className={styles.title}>{p.title}</span>
          </NavLink>
        ))}
      </nav>
      {projectId && (
        <div className={styles.pageLinks}>
          <NavLink
            to={`/projects/${projectId}/lore`}
            className={({ isActive }) =>
              isActive ? `${styles.item} ${styles.active}` : styles.item
            }
          >
            <span className={styles.title}>设定</span>
          </NavLink>
          <NavLink
            to={`/projects/${projectId}/settings`}
            className={({ isActive }) =>
              isActive ? `${styles.item} ${styles.active}` : styles.item
            }
          >
            <span className={styles.title}>创作设置</span>
          </NavLink>
          <NavLink
            to={`/projects/${projectId}/audit`}
            className={({ isActive }) =>
              isActive ? `${styles.item} ${styles.active}` : styles.item
            }
          >
            <span className={styles.title}>全局审计</span>
          </NavLink>
        </div>
      )}
      {!projectId && (
        <nav className={styles.settings} aria-label="全局设置">
          <NavLink
            to="/environment"
            className={({ isActive }) => (isActive ? `${styles.item} ${styles.active}` : styles.item)}
          >
            <span className={styles.title}>环境配置</span>
          </NavLink>
          <NavLink
            to="/theme"
            className={({ isActive }) => (isActive ? `${styles.item} ${styles.active}` : styles.item)}
          >
            <span className={styles.title}>主题</span>
          </NavLink>
        </nav>
      )}
      <div className={styles.foot}>
        <div className={styles.userRow}>
          <span className={styles.user}>{session?.username}</span>
          <button type="button" className="btn btn-quiet" onClick={onLogout}>
            登出
          </button>
        </div>
      </div>
    </aside>
  )
}
