// 项目库：左 rail + 「新建作品」+ 项目卡（title/genre/current_chapter → 进入工作台）。
import { useCallback, useEffect, useState } from 'react'
import type { MouseEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ProjectRail } from '../components/ProjectRail'
import { useAuth } from '../context/AuthContext'
import { api, ApiError } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import { isProjectDraft, projectHref } from '../lib/projectCreation'
import type { Project } from '../types'
import styles from './ProjectsPage.module.css'

export default function ProjectsPage() {
  const { logout } = useAuth()
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  // 删除/重载共用：删除成功后刷新列表
  const loadProjects = useCallback(() => {
    setError(null)
    api
      .listProjects()
      .then((list) => setProjects(list))
      .catch((err) => setError(formatApiError(err, '加载失败')))
  }, [])

  useEffect(() => {
    void loadProjects()
  }, [loadProjects])

  // 整本书删除（阶段 6 硬删）：卡片是整块 <Link>，删除按钮必须截断冒泡防误导航。
  // 有进行中任务 → Python 409（本书需先暂停/取消），前端给中文横幅。
  const handleDelete = useCallback(
    (e: MouseEvent, p: Project) => {
      e.preventDefault()
      e.stopPropagation()
      if (!window.confirm(`删除《${p.title}》？全书正文、记忆、向量与任务记录将一并移除，不可撤销。`)) return
      api
        .deleteProject(p.id)
        .then(() => {
          setDeleteError(null)
          void loadProjects()
        })
        .catch((err) => {
          if (err instanceof ApiError && err.status === 409) {
            setDeleteError(`《${p.title}》有进行中任务，无法删除。请先暂停/取消后再试。`)
          } else {
            setDeleteError(formatApiError(err, '删除失败'))
          }
        })
    },
    [loadProjects],
  )

  return (
    <div className={styles.wrap}>
      <ProjectRail projects={projects ?? []} onLogout={logout} />
      <main className={styles.main}>
        <div className={styles.head}>
          <h1>作品库</h1>
          <button type="button" className="btn btn-primary" onClick={() => navigate('/projects/new')}>
            新建作品
          </button>
        </div>
        {error && <div className="banner banner-error">加载失败：{error}</div>}
        {deleteError && <div className="banner banner-error">{deleteError}</div>}
        {projects === null ? (
          <div className="empty">加载中…</div>
        ) : projects.length === 0 ? (
          <div className="empty">还没有作品。点击右上角「新建作品」，一句话梗概即可创建第一本书。</div>
        ) : (
          <>
          <h2 className={styles.sectionTitle}>正式作品</h2>
          {projects.every(isProjectDraft) && <p className="empty">尚无已完成建书的作品，请先完成下方草稿。</p>}
          <div className={styles.grid}>
            {projects.filter((p) => !isProjectDraft(p)).map((p) => (
              <Link key={p.id} to={projectHref(p)} className={`panel ${styles.card}`}>
                <h2 className={styles.title}>{p.title}</h2>
                <div className={styles.meta}>
                  {p.genre} · 已写至第 {p.current_chapter} 章
                </div>
                <button
                  type="button"
                  className={styles.del}
                  onClick={(e) => handleDelete(e, p)}
                  aria-label={`删除《${p.title}》`}
                  title="删除本书"
                >
                  删除
                </button>
              </Link>
            ))}
          </div>
          {projects.some(isProjectDraft) && (
            <section aria-label="待完成作品">
              <h2 className={styles.sectionTitle}>待完成作品</h2>
              <p className={styles.meta}>草稿已保留。确认设定和整书大纲后，才会进入正式作品并开放写作。</p>
              <div className={styles.grid}>
                {projects.filter(isProjectDraft).map((p) => (
                  <Link key={p.id} to={projectHref(p)} className={`panel ${styles.card}`}>
                    <h3>{p.title}</h3>
                    <span className={styles.meta}>{p.creation_status === 'setup_confirmed' ? '设定已确认 · 待确认大纲' : '待完成设定与大纲'}</span>
                    <span>继续创建 →</span>
                  </Link>
                ))}
              </div>
            </section>
          )}
          </>
        )}
      </main>
    </div>
  )
}
