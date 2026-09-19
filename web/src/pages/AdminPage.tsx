import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react'
import { ProjectRail } from '../components/ProjectRail'
import { useAuth } from '../context/AuthContext'
import { ApiError } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import {
  adminApi,
  type AdminAccessLog,
  type AdminChapterDetail,
  type AdminContext,
  type AdminMetrics,
  type AdminOverview,
  type AdminPage as PageResult,
  type AdminProject,
  type AdminRun,
  type AdminRunDetail,
  type AdminTask,
  type AdminTaskDetail,
  type AdminUser,
  type CapturedData,
} from '../lib/adminApi'
import styles from './AdminPage.module.css'

const PAGE_SIZE = 25
type Tab = 'overview' | 'users' | 'projects' | 'tasks' | 'runs' | 'logs'

function useResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  onForbidden: () => void,
) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const sequence = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    const current = ++sequence.current
    setLoading(true)
    setError(null)
    setData(null)
    void loader(controller.signal).then((value) => {
      if (!controller.signal.aborted && current === sequence.current) setData(value)
    }).catch((reason: unknown) => {
      if (controller.signal.aborted || current !== sequence.current) return
      if (reason instanceof ApiError && reason.status === 403) {
        onForbidden()
        return
      }
      if (reason instanceof ApiError && reason.code === 'request_aborted') return
      setError(formatApiError(reason, '加载失败，请稍后重试'))
    }).finally(() => {
      if (!controller.signal.aborted && current === sequence.current) setLoading(false)
    })
    return () => controller.abort()
  }, [loader, onForbidden, revision])

  return { data, loading, error, retry: () => setRevision((value) => value + 1) }
}

function LoadState({
  loading,
  error,
  empty,
  retry,
  children,
}: {
  loading: boolean
  error: string | null
  empty: boolean
  retry: () => void
  children: ReactNode
}) {
  if (loading) return <div className="empty" role="status">正在加载…</div>
  if (error) {
    return (
      <div className={`banner banner-error ${styles.loadError}`} role="alert">
        <span>{error}</span>
        <button type="button" className="btn btn-secondary" onClick={retry}>重试</button>
      </div>
    )
  }
  if (empty) return <div className="empty">暂无数据</div>
  return children
}

function Pagination({ total, offset, onChange }: {
  total: number
  offset: number
  onChange: (offset: number) => void
}) {
  return (
    <div className={styles.pagination} aria-label="分页">
      <button
        type="button"
        className="btn btn-quiet"
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}
      >上一页</button>
      <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页 · 共 {total} 条</span>
      <button
        type="button"
        className="btn btn-quiet"
        disabled={offset + PAGE_SIZE >= total}
        onClick={() => onChange(offset + PAGE_SIZE)}
      >下一页</button>
    </div>
  )
}

function RefreshButton({ onClick }: { onClick: () => void }) {
  return <button type="button" className="btn btn-secondary" onClick={onClick}>刷新当前视图</button>
}

function Metrics({ value }: { value: AdminMetrics }) {
  return (
    <dl className={styles.metrics}>
      <div><dt>运行数</dt><dd>{value.run_count.toLocaleString()}</dd></div>
      <div><dt>输入 Token</dt><dd>{value.input_tokens.toLocaleString()}</dd></div>
      <div><dt>输出 Token</dt><dd>{value.output_tokens.toLocaleString()}</dd></div>
      <div><dt>预估存储成本（非实际账单）</dt><dd>¥/US$ {value.cost_est.toFixed(4)}</dd></div>
      <div><dt>节点计时合计；0 表示未记录</dt><dd>{formatDuration(value.duration_ms)}</dd></div>
    </dl>
  )
}

function formatDuration(value: number): string {
  if (value === 0) return '0（未记录）'
  if (value < 1000) return `${value} ms`
  return `${(value / 1000).toFixed(2)} 秒`
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function JsonText({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === '') return <p className={styles.muted}>无记录</p>
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  return <pre className={styles.pre}>{text}</pre>
}

function CapturedView({ value, label }: { value: CapturedData; label: string }) {
  return (
    <section className={styles.capture} aria-label={label}>
      <h4>{label}</h4>
      <div className={styles.flags}>
        {value.truncated && <span className="badge badge-warning">内容已截断</span>}
        {value.redacted && <span className="badge badge-warning">敏感信息已脱敏</span>}
        {!value.truncated && !value.redacted && <span className="badge">完整的受限快照</span>}
      </div>
      <JsonText value={value.data} />
      <small className={styles.muted}>
        上限：{value.limits.max_items} 项 / {value.limits.max_text} 字符 / {value.limits.max_bytes} 字节
      </small>
    </section>
  )
}

function OverviewView({ token, onForbidden }: { token: string; onForbidden: () => void }) {
  const load = useCallback((signal: AbortSignal) => adminApi.getOverview(token, signal), [token])
  const resource = useResource<AdminOverview>(load, onForbidden)
  const value = resource.data
  return (
    <section className={styles.view} aria-labelledby="overview-heading">
      <div className={styles.viewHead}>
        <div><h2 id="overview-heading">全局概览</h2><p>只读统计，来自当前服务端数据。</p></div>
        <RefreshButton onClick={resource.retry} />
      </div>
      <LoadState {...resource} empty={!value}>
        {value && (
          <>
            <dl className={styles.counts}>
              <div><dt>用户</dt><dd>{value.user_count}</dd></div>
              <div><dt>作品</dt><dd>{value.project_count}</dd></div>
              <div><dt>章节</dt><dd>{value.chapter_count}</dd></div>
              <div><dt>任务</dt><dd>{value.task_count}</dd></div>
            </dl>
            <div className={`panel ${styles.panel}`}><h3>运行指标</h3><Metrics value={value.metrics} /></div>
            <div className={`panel ${styles.panel}`}>
              <h3>任务状态</h3>
              {Object.keys(value.task_status_counts).length === 0
                ? <div className="empty">暂无状态统计</div>
                : <dl className={styles.statusCounts}>{Object.entries(value.task_status_counts).map(([name, count]) => (
                  <div key={name}><dt>{name}</dt><dd>{count}</dd></div>
                ))}</dl>}
            </div>
          </>
        )}
      </LoadState>
    </section>
  )
}

function UsersView({ token, onForbidden, onProjects, onTasks }: {
  token: string
  onForbidden: () => void
  onProjects: (userId: string) => void
  onTasks: (userId: string) => void
}) {
  const [draft, setDraft] = useState('')
  const [q, setQ] = useState('')
  const [offset, setOffset] = useState(0)
  const load = useCallback(
    (signal: AbortSignal) => adminApi.listUsers(token, { q, limit: PAGE_SIZE, offset }, signal),
    [offset, q, token],
  )
  const resource = useResource<PageResult<AdminUser>>(load, onForbidden)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setOffset(0)
    setQ(draft.trim())
  }
  return (
    <section className={styles.view} aria-labelledby="users-heading">
      <div className={styles.viewHead}><div><h2 id="users-heading">用户</h2><p>搜索账号并查看统计。</p></div><RefreshButton onClick={resource.retry} /></div>
      <form className={styles.filters} role="search" aria-label="用户筛选" onSubmit={submit}>
        <label><span>搜索用户</span><input className="input" value={draft} maxLength={128} onChange={(event) => setDraft(event.target.value)} /></label>
        <button className="btn btn-primary" type="submit">搜索</button>
      </form>
      <LoadState {...resource} empty={resource.data?.items.length === 0}>
        {resource.data && <>
          <div className={styles.tableWrap}><table><thead><tr><th>用户</th><th>角色/级别</th><th>作品/章节/字数</th><th>任务/运行</th><th>操作</th></tr></thead>
            <tbody>{resource.data.items.map((user) => <tr key={user.id}>
              <td><strong>{user.username}</strong><small>{user.id}</small></td>
              <td>{user.role} / {user.tier}</td>
              <td>{user.project_count} / {user.chapter_count} / {user.word_count.toLocaleString()}</td>
              <td>{user.task_count} / {user.metrics.run_count}</td>
              <td className={styles.actions}>
                <button type="button" className="btn btn-quiet" aria-label={`查看 ${user.username} 的作品`} onClick={() => onProjects(user.id)}>作品</button>
                <button type="button" className="btn btn-quiet" aria-label={`查看 ${user.username} 的任务`} onClick={() => onTasks(user.id)}>任务</button>
              </td>
            </tr>)}</tbody></table></div>
          <Pagination total={resource.data.total} offset={offset} onChange={setOffset} />
        </>}
      </LoadState>
    </section>
  )
}

function ChapterDetailView({ token, projectId, chapterId, onForbidden }: {
  token: string
  projectId: string
  chapterId: string
  onForbidden: () => void
}) {
  const load = useCallback(
    (signal: AbortSignal) => adminApi.getChapter(token, projectId, chapterId, signal),
    [chapterId, projectId, token],
  )
  const resource = useResource<AdminChapterDetail>(load, onForbidden)
  return (
    <LoadState {...resource} empty={!resource.data}>
      {resource.data && <article className={`panel ${styles.detail}`}>
        <h3>{resource.data.title ?? `第 ${resource.data.chapter_seq} 章`} · v{resource.data.version}</h3>
        <h4>摘要</h4><JsonText value={resource.data.summary} />
        <h4>全文</h4><JsonText value={resource.data.content} />
      </article>}
    </LoadState>
  )
}

function ContextView({ value }: { value: AdminContext }) {
  const collections: Array<[string, AdminContext[keyof Pick<AdminContext,
    'outlines' | 'events' | 'facts' | 'characters' | 'foreshadows' | 'threads'
  >]]> = [
    ['大纲', value.outlines], ['事件', value.events], ['事实', value.facts],
    ['人物', value.characters], ['伏笔', value.foreshadows], ['故事线', value.threads],
  ]
  return (
    <div className={styles.context}>
      <CapturedView value={value.settings} label="作品设置" />
      {collections.map(([label, collection]) => <section className={styles.capture} key={label}>
        <h4>{label}（显示 {collection.items.length} / 共 {collection.total}）</h4>
        {collection.truncated && <span className="badge badge-warning">集合已截断</span>}
        {collection.items.length === 0
          ? <p className={styles.muted}>暂无记录</p>
          : collection.items.map((item, index) => <CapturedView key={`${label}-${index}`} value={item} label={`${label} ${index + 1}`} />)}
      </section>)}
    </div>
  )
}

function ProjectDetailView({ token, project, onForbidden }: {
  token: string
  project: AdminProject
  onForbidden: () => void
}) {
  const [offset, setOffset] = useState(0)
  const [chapterId, setChapterId] = useState<string | null>(null)
  const loadChapters = useCallback(
    (signal: AbortSignal) => adminApi.listChapters(
      token, project.id, { limit: PAGE_SIZE, offset }, signal,
    ),
    [offset, project.id, token],
  )
  const loadContext = useCallback(
    (signal: AbortSignal) => adminApi.getProjectContext(token, project.id, PAGE_SIZE, signal),
    [project.id, token],
  )
  const chapters = useResource(loadChapters, onForbidden)
  const context = useResource(loadContext, onForbidden)
  return (
    <div className={styles.drilldown}>
      <div className={styles.detailHead}>
        <div><h3>《{project.title}》</h3><p>{project.username} · {project.genre} · {project.creation_status}</p></div>
        <div><RefreshButton onClick={chapters.retry} /> <button type="button" className="btn btn-secondary" onClick={context.retry}>刷新设定</button></div>
      </div>
      <div className={styles.split}>
        <section className={`panel ${styles.detail}`}>
          <h3>章节元数据</h3>
          <LoadState {...chapters} empty={chapters.data?.items.length === 0}>
            {chapters.data && <>
              <ul className={styles.list}>{chapters.data.items.map((chapter) => <li key={chapter.id}>
                <div><strong>{chapter.title ?? `第 ${chapter.chapter_seq} 章`}</strong><small>{chapter.status} · {chapter.word_count.toLocaleString()} 字 · {formatDate(chapter.updated_at)}</small></div>
                <button type="button" className="btn btn-quiet" aria-label={`查看${chapter.title ?? `第 ${chapter.chapter_seq} 章`}全文`} onClick={() => setChapterId(chapter.id)}>全文</button>
              </li>)}</ul>
              <Pagination total={chapters.data.total} offset={offset} onChange={(next) => { setChapterId(null); setOffset(next) }} />
            </>}
          </LoadState>
        </section>
        <section className={`panel ${styles.detail}`}>
          <h3>受限上下文快照</h3>
          <p className={styles.muted}>所有字段均可能被截断或脱敏；这里只显示服务端允许的设置、大纲、事件、事实和上下文。</p>
          <LoadState {...context} empty={!context.data}>{context.data && <ContextView value={context.data} />}</LoadState>
        </section>
      </div>
      {chapterId && <ChapterDetailView key={chapterId} token={token} projectId={project.id} chapterId={chapterId} onForbidden={onForbidden} />}
    </div>
  )
}

function ProjectsView({ token, onForbidden, initialUserId }: {
  token: string
  onForbidden: () => void
  initialUserId: string
}) {
  const [draftQ, setDraftQ] = useState('')
  const [draftUser, setDraftUser] = useState(initialUserId)
  const [filters, setFilters] = useState({ q: '', userId: initialUserId })
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<AdminProject | null>(null)
  const load = useCallback(
    (signal: AbortSignal) => adminApi.listProjects(token, { ...filters, limit: PAGE_SIZE, offset }, signal),
    [filters, offset, token],
  )
  const resource = useResource<PageResult<AdminProject>>(load, onForbidden)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setSelected(null)
    setOffset(0)
    setFilters({ q: draftQ.trim(), userId: draftUser.trim() })
  }
  return (
    <section className={styles.view} aria-labelledby="projects-heading">
      <div className={styles.viewHead}><div><h2 id="projects-heading">作品</h2><p>按作品名或作者查看统计、章节和受限上下文。</p></div><RefreshButton onClick={resource.retry} /></div>
      <form className={styles.filters} role="search" aria-label="作品筛选" onSubmit={submit}>
        <label><span>作品搜索</span><input className="input" value={draftQ} maxLength={128} onChange={(event) => setDraftQ(event.target.value)} /></label>
        <label><span>用户 ID</span><input className="input" value={draftUser} onChange={(event) => setDraftUser(event.target.value)} /></label>
        <button className="btn btn-primary" type="submit">筛选</button>
      </form>
      <LoadState {...resource} empty={resource.data?.items.length === 0}>
        {resource.data && <>
          <div className={styles.tableWrap}><table><thead><tr><th>作品</th><th>作者</th><th>状态</th><th>章节/字数/任务</th><th>运行/预估成本</th><th>操作</th></tr></thead>
            <tbody>{resource.data.items.map((project) => <tr key={project.id}>
              <td><strong>{project.title}</strong><small>{project.id}</small></td><td>{project.username}</td>
              <td>{project.creation_status}</td><td>{project.chapter_count} / {project.word_count.toLocaleString()} / {project.task_count}</td>
              <td>{project.metrics.run_count} / {project.metrics.cost_est.toFixed(4)}</td>
              <td><button type="button" className="btn btn-quiet" aria-label={`查看《${project.title}》`} onClick={() => setSelected(project)}>查看</button></td>
            </tr>)}</tbody></table></div>
          <Pagination total={resource.data.total} offset={offset} onChange={(next) => { setSelected(null); setOffset(next) }} />
        </>}
      </LoadState>
      {selected && <ProjectDetailView key={selected.id} token={token} project={selected} onForbidden={onForbidden} />}
    </section>
  )
}

function RunTable({ runs, onSelect }: { runs: AdminRun[]; onSelect: (run: AdminRun) => void }) {
  return (
    <div className={styles.tableWrap}><table><thead><tr><th># / 节点</th><th>作品/任务</th><th>模型</th><th>Token</th><th>预估成本</th><th>计时</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{runs.map((run) => <tr key={run.id}>
        <td><strong>#{run.id} {run.node}</strong><small>{formatDate(run.created_at)}</small></td>
        <td>{run.project_title}<small>{run.task_id ?? '无关联任务'}</small></td>
        <td>{run.model_id ?? '未记录'}<small>{run.role ?? '未记录角色'}</small></td>
        <td>{run.input_tokens.toLocaleString()} / {run.output_tokens.toLocaleString()}</td>
        <td>{run.cost_est.toFixed(4)}（估算）</td>
        <td>{formatDuration(run.duration_ms)}{run.duration_ms === 0 && <small>0 表示未记录</small>}</td>
        <td>{run.degraded ? '降级' : '未降级'} · 重试 {run.retry_count}</td>
        <td><button type="button" className="btn btn-quiet" aria-label={`查看节点 ${run.node} #${run.id}`} onClick={() => onSelect(run)}>详情</button></td>
      </tr>)}</tbody></table></div>
  )
}

function RunDetailView({ token, runId, onForbidden }: {
  token: string
  runId: number
  onForbidden: () => void
}) {
  const load = useCallback(
    (signal: AbortSignal) => adminApi.getRun(token, runId, signal),
    [runId, token],
  )
  const resource = useResource<AdminRunDetail>(load, onForbidden)
  return (
    <LoadState {...resource} empty={!resource.data}>
      {resource.data && <article className={`panel ${styles.detail}`}>
        <div className={styles.detailHead}><div><h3>节点 {resource.data.node} #{resource.data.id}</h3><p>{resource.data.model_id ?? '未记录模型'} · {formatDuration(resource.data.duration_ms)}</p></div><RefreshButton onClick={resource.retry} /></div>
        {resource.data.duration_ms === 0 && <p className="banner banner-warning">节点时长 0 表示未记录，不代表瞬时完成。</p>}
        {resource.data.detail_missing && <p className="banner banner-warning">旧记录未保存详情，无法重建。</p>}
        {resource.data.prompt_missing && <p className="banner banner-warning">旧记录未保存提示词，无法重建。</p>}
        <CapturedView value={resource.data.detail} label="实际调试详情（提示词、输出、审计与召回片段）" />
        <CapturedView value={resource.data.error} label="错误详情" />
      </article>}
    </LoadState>
  )
}

function TaskDetailView({ token, task, onForbidden }: {
  token: string
  task: AdminTask
  onForbidden: () => void
}) {
  const [offset, setOffset] = useState(0)
  const [runId, setRunId] = useState<number | null>(null)
  const loadTask = useCallback(
    (signal: AbortSignal) => adminApi.getTask(token, task.id, signal),
    [task.id, token],
  )
  const loadRuns = useCallback(
    (signal: AbortSignal) => adminApi.listTaskRuns(token, task.id, { limit: PAGE_SIZE, offset }, signal),
    [offset, task.id, token],
  )
  const detail = useResource<AdminTaskDetail>(loadTask, onForbidden)
  const runs = useResource<PageResult<AdminRun>>(loadRuns, onForbidden)
  return (
    <div className={styles.drilldown}>
      <section className={`panel ${styles.detail}`}>
        <div className={styles.detailHead}><div><h3>任务 {task.id}</h3><p>{task.project_title} · {task.task_type} · {detail.data?.status ?? task.status}</p></div><RefreshButton onClick={detail.retry} /></div>
        <LoadState {...detail} empty={!detail.data}>{detail.data && <>
          <dl className={styles.summaryList}>
            <div><dt>创建/更新</dt><dd>{formatDate(detail.data.created_at)} / {formatDate(detail.data.updated_at)}</dd></div>
            <div><dt>任务跨度</dt><dd>{formatDuration(detail.data.elapsed_ms)} <small>包含排队、暂停与人工等待</small></dd></div>
            <div><dt>章节/批次</dt><dd>{detail.data.chapter_seq ?? '无'} / {detail.data.batch_task_id ?? '无'}</dd></div>
          </dl>
          <CapturedView value={detail.data.payload} label="任务载荷" />
          <CapturedView value={detail.data.error} label="任务错误" />
        </>}</LoadState>
      </section>
      <section className={`panel ${styles.detail}`}>
        <div className={styles.detailHead}><div><h3>有序节点流</h3><p>按运行 ID 升序；节点计时不等于任务跨度。</p></div><RefreshButton onClick={runs.retry} /></div>
        <LoadState {...runs} empty={runs.data?.items.length === 0}>
          {runs.data && <><RunTable runs={runs.data.items} onSelect={(run) => setRunId(run.id)} /><Pagination total={runs.data.total} offset={offset} onChange={(next) => { setRunId(null); setOffset(next) }} /></>}
        </LoadState>
      </section>
      {runId !== null && <RunDetailView key={runId} token={token} runId={runId} onForbidden={onForbidden} />}
    </div>
  )
}

function TasksView({ token, onForbidden, initialUserId }: {
  token: string
  onForbidden: () => void
  initialUserId: string
}) {
  const [draft, setDraft] = useState({ userId: initialUserId, projectId: '', status: '' })
  const [filters, setFilters] = useState(draft)
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<AdminTask | null>(null)
  const load = useCallback(
    (signal: AbortSignal) => adminApi.listTasks(token, { ...filters, limit: PAGE_SIZE, offset }, signal),
    [filters, offset, token],
  )
  const resource = useResource<PageResult<AdminTask>>(load, onForbidden)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setSelected(null)
    setOffset(0)
    setFilters({
      userId: draft.userId.trim(), projectId: draft.projectId.trim(), status: draft.status.trim(),
    })
  }
  return (
    <section className={styles.view} aria-labelledby="tasks-heading">
      <div className={styles.viewHead}><div><h2 id="tasks-heading">任务</h2><p>按用户、作品和状态筛选。</p></div><RefreshButton onClick={resource.retry} /></div>
      <form className={styles.filters} aria-label="任务筛选" onSubmit={submit}>
        <label><span>用户 ID</span><input className="input" value={draft.userId} onChange={(event) => setDraft({ ...draft, userId: event.target.value })} /></label>
        <label><span>作品 ID</span><input className="input" value={draft.projectId} onChange={(event) => setDraft({ ...draft, projectId: event.target.value })} /></label>
        <label><span>状态</span><input className="input" maxLength={32} value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value })} /></label>
        <button className="btn btn-primary" type="submit">筛选</button>
      </form>
      <LoadState {...resource} empty={resource.data?.items.length === 0}>
        {resource.data && <>
          <div className={styles.tableWrap}><table><thead><tr><th>任务</th><th>用户/作品</th><th>类型/状态</th><th>章节/重试</th><th>运行/Token</th><th>操作</th></tr></thead>
            <tbody>{resource.data.items.map((task) => <tr key={task.id}>
              <td><strong>{task.id}</strong><small>{formatDate(task.created_at)}</small></td><td>{task.username}<small>{task.project_title}</small></td>
              <td>{task.task_type} / {task.status}</td><td>{task.chapter_seq ?? '无'} / {task.retry_count}</td>
              <td>{task.metrics.run_count} / {(task.metrics.input_tokens + task.metrics.output_tokens).toLocaleString()}</td>
              <td><button type="button" className="btn btn-quiet" aria-label={`查看任务 ${task.id}`} onClick={() => setSelected(task)}>查看</button></td>
            </tr>)}</tbody></table></div>
          <Pagination total={resource.data.total} offset={offset} onChange={(next) => { setSelected(null); setOffset(next) }} />
        </>}
      </LoadState>
      {selected && <TaskDetailView key={selected.id} token={token} task={selected} onForbidden={onForbidden} />}
    </section>
  )
}

function RunsView({ token, onForbidden }: { token: string; onForbidden: () => void }) {
  const [draft, setDraft] = useState({ userId: '', projectId: '', node: '' })
  const [filters, setFilters] = useState(draft)
  const [offset, setOffset] = useState(0)
  const [runId, setRunId] = useState<number | null>(null)
  const load = useCallback(
    (signal: AbortSignal) => adminApi.listRuns(token, { ...filters, limit: PAGE_SIZE, offset }, signal),
    [filters, offset, token],
  )
  const resource = useResource<PageResult<AdminRun>>(load, onForbidden)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setRunId(null)
    setOffset(0)
    setFilters({
      userId: draft.userId.trim(), projectId: draft.projectId.trim(), node: draft.node.trim(),
    })
  }
  return (
    <section className={styles.view} aria-labelledby="runs-heading">
      <div className={styles.viewHead}><div><h2 id="runs-heading">全部运行</h2><p>包含建书、大纲等没有任务 ID 的运行。</p></div><RefreshButton onClick={resource.retry} /></div>
      <form className={styles.filters} aria-label="运行筛选" onSubmit={submit}>
        <label><span>用户 ID</span><input className="input" value={draft.userId} onChange={(event) => setDraft({ ...draft, userId: event.target.value })} /></label>
        <label><span>作品 ID</span><input className="input" value={draft.projectId} onChange={(event) => setDraft({ ...draft, projectId: event.target.value })} /></label>
        <label><span>节点</span><input className="input" maxLength={64} value={draft.node} onChange={(event) => setDraft({ ...draft, node: event.target.value })} /></label>
        <button className="btn btn-primary" type="submit">筛选</button>
      </form>
      <LoadState {...resource} empty={resource.data?.items.length === 0}>
        {resource.data && <><RunTable runs={resource.data.items} onSelect={(run) => setRunId(run.id)} /><Pagination total={resource.data.total} offset={offset} onChange={(next) => { setRunId(null); setOffset(next) }} /></>}
      </LoadState>
      {runId !== null && <RunDetailView key={runId} token={token} runId={runId} onForbidden={onForbidden} />}
    </section>
  )
}

function LogsView({ token, onForbidden }: { token: string; onForbidden: () => void }) {
  const [offset, setOffset] = useState(0)
  const load = useCallback(
    (signal: AbortSignal) => adminApi.listAccessLogs(token, { limit: PAGE_SIZE, offset }, signal),
    [offset, token],
  )
  const resource = useResource<PageResult<AdminAccessLog>>(load, onForbidden)
  return (
    <section className={styles.view} aria-labelledby="logs-heading">
      <div className={styles.viewHead}><div><h2 id="logs-heading">访问日志</h2><p>只记录管理员读取动作和目标，不展示凭据。</p></div><RefreshButton onClick={resource.retry} /></div>
      <LoadState {...resource} empty={resource.data?.items.length === 0}>
        {resource.data && <>
          <div className={styles.tableWrap}><table><thead><tr><th>时间</th><th>管理员</th><th>动作</th><th>目标</th></tr></thead>
            <tbody>{resource.data.items.map((entry) => <tr key={entry.id}><td>{formatDate(entry.created_at)}</td><td>{entry.actor_id}</td><td>{entry.action}</td><td>{entry.target}</td></tr>)}</tbody></table></div>
          <Pagination total={resource.data.total} offset={offset} onChange={setOffset} />
        </>}
      </LoadState>
    </section>
  )
}

function AdminConsole({ token, logout, onForbidden }: {
  token: string
  logout: () => Promise<void>
  onForbidden: () => void
}) {
  const [tab, setTab] = useState<Tab>('overview')
  const [userFilter, setUserFilter] = useState('')
  const tabs: Array<[Tab, string]> = [
    ['overview', '概览'], ['users', '用户'], ['projects', '作品'], ['tasks', '任务'],
    ['runs', '全部运行'], ['logs', '访问日志'],
  ]
  const openProjects = (userId: string) => { setUserFilter(userId); setTab('projects') }
  const openTasks = (userId: string) => { setUserFilter(userId); setTab('tasks') }
  return (
    <div className={styles.wrap}>
      <ProjectRail projects={[]} onLogout={logout} />
      <main className={styles.main}>
        <header className={styles.header}>
          <div><h1>管理员控制台</h1><p>跨账号只读观测；服务端权限仍是最终安全边界。</p></div>
          <span className="badge badge-warning">只读</span>
        </header>
        <nav className={styles.tabs} aria-label="管理视图">
          {tabs.map(([id, label]) => <button
            key={id}
            type="button"
            className={tab === id ? styles.activeTab : ''}
            aria-pressed={tab === id}
            onClick={() => { setUserFilter(''); setTab(id) }}
          >{label}</button>)}
        </nav>
        {tab === 'overview' && <OverviewView token={token} onForbidden={onForbidden} />}
        {tab === 'users' && <UsersView token={token} onForbidden={onForbidden} onProjects={openProjects} onTasks={openTasks} />}
        {tab === 'projects' && <ProjectsView key={`projects:${userFilter}`} token={token} onForbidden={onForbidden} initialUserId={userFilter} />}
        {tab === 'tasks' && <TasksView key={`tasks:${userFilter}`} token={token} onForbidden={onForbidden} initialUserId={userFilter} />}
        {tab === 'runs' && <RunsView token={token} onForbidden={onForbidden} />}
        {tab === 'logs' && <LogsView token={token} onForbidden={onForbidden} />}
      </main>
    </div>
  )
}

export default function AdminPage() {
  const { session, status, revalidate, logout } = useAuth()
  const [forbidden, setForbidden] = useState(false)
  const identity = session ? `${session.userId}:${session.token}` : ''
  useEffect(() => setForbidden(false), [identity])
  const onForbidden = useCallback(() => {
    setForbidden(true)
    revalidate()
  }, [revalidate])

  const allowed = status === 'authenticated'
    && session?.role === 'admin'
    && session.roleVerified === true
  if (!allowed || !session) return null
  if (forbidden) {
    return (
      <div className={styles.denied} role="alert">
        <h1>管理员权限已变化</h1>
        <p>受保护数据已清除，正在重新验证当前会话。</p>
      </div>
    )
  }
  return (
    <AdminConsole
      key={identity}
      token={session.token}
      logout={logout}
      onForbidden={onForbidden}
    />
  )
}
