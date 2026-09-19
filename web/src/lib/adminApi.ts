import { authenticatedGet } from './api'

export interface AdminPage<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface AdminMetrics {
  run_count: number
  input_tokens: number
  output_tokens: number
  cost_est: number
  duration_ms: number
}

export interface CaptureLimits {
  max_depth: number
  max_items: number
  max_text: number
  max_bytes: number
}

export interface CapturedData {
  data: unknown
  truncated: boolean
  redacted: boolean
  limits: CaptureLimits
}

export interface AdminOverview {
  user_count: number
  project_count: number
  chapter_count: number
  task_count: number
  metrics: AdminMetrics
  task_status_counts: Record<string, number>
}

export interface AdminUser {
  id: string
  username: string
  tier: string
  role: 'user' | 'admin'
  project_count: number
  chapter_count: number
  word_count: number
  task_count: number
  metrics: AdminMetrics
}

export interface AdminProject {
  id: string
  user_id: string
  username: string
  title: string
  genre: string
  current_chapter: number
  target_words: number | null
  creation_status: string
  created_at: string
  updated_at: string
  chapter_count: number
  word_count: number
  task_count: number
  metrics: AdminMetrics
}

export interface AdminChapter {
  id: string
  project_id: string
  chapter_seq: number
  title: string | null
  status: string
  word_count: number
  created_at: string
  updated_at: string
}

export interface AdminChapterDetail extends AdminChapter {
  content: string | null
  summary: string | null
  version: number
}

export interface AdminContextCollection {
  items: CapturedData[]
  total: number
  limit: number
  truncated: boolean
}

export interface AdminContext {
  project_id: string
  settings: CapturedData
  outlines: AdminContextCollection
  events: AdminContextCollection
  facts: AdminContextCollection
  characters: AdminContextCollection
  foreshadows: AdminContextCollection
  threads: AdminContextCollection
}

export interface AdminTask {
  id: string
  project_id: string
  user_id: string
  username: string
  project_title: string
  task_type: string
  status: string
  chapter_seq: number | null
  batch_task_id: string | null
  retry_count: number
  created_at: string
  updated_at: string
  metrics: AdminMetrics
}

export interface AdminTaskDetail extends AdminTask {
  payload: CapturedData
  error: CapturedData
  elapsed_ms: number
  elapsed_includes_waits: true
}

export interface AdminRun {
  id: number
  project_id: string
  user_id: string
  username: string
  project_title: string
  task_id: string | null
  node: string
  role: string | null
  model_id: string | null
  input_tokens: number
  output_tokens: number
  cost_est: number
  duration_ms: number
  cache_hit: boolean
  degraded: boolean
  retry_count: number
  created_at: string
  updated_at: string
}

export interface AdminRunDetail extends AdminRun {
  detail: CapturedData
  error: CapturedData
  detail_missing: boolean
  prompt_missing: boolean
}

export interface AdminAccessLog {
  id: number
  actor_id: string
  action: string
  target: string
  created_at: string
}

interface PageFilters {
  limit?: number
  offset?: number
}

interface ProjectFilters extends PageFilters {
  userId?: string
  q?: string
}

interface TaskFilters extends PageFilters {
  userId?: string
  projectId?: string
  status?: string
}

interface RunFilters extends PageFilters {
  userId?: string
  projectId?: string
  node?: string
}

function query(entries: Array<[string, string | number | undefined]>): string {
  const params = new URLSearchParams()
  for (const [key, value] of entries) {
    if (value !== undefined && value !== '') params.set(key, String(value))
  }
  const result = params.toString()
  return result ? `?${result}` : ''
}

const paging = (filters: PageFilters): Array<[string, string | number | undefined]> => [
  ['limit', filters.limit ?? 25], ['offset', filters.offset ?? 0],
]

export const adminApi = {
  getOverview: (token: string, signal?: AbortSignal) =>
    authenticatedGet<AdminOverview>('/admin/overview', token, signal),

  listUsers: (token: string, filters: PageFilters & { q?: string }, signal?: AbortSignal) =>
    authenticatedGet<AdminPage<AdminUser>>(`/admin/users${query([
      ['q', filters.q], ...paging(filters),
    ])}`, token, signal),

  listProjects: (token: string, filters: ProjectFilters, signal?: AbortSignal) =>
    authenticatedGet<AdminPage<AdminProject>>(`/admin/projects${query([
      ['user_id', filters.userId], ['q', filters.q], ...paging(filters),
    ])}`, token, signal),

  listChapters: (token: string, projectId: string, filters: PageFilters, signal?: AbortSignal) =>
    authenticatedGet<AdminPage<AdminChapter>>(
      `/admin/projects/${encodeURIComponent(projectId)}/chapters${query(paging(filters))}`,
      token, signal,
    ),

  getChapter: (token: string, projectId: string, chapterId: string, signal?: AbortSignal) =>
    authenticatedGet<AdminChapterDetail>(
      `/admin/projects/${encodeURIComponent(projectId)}/chapters/${encodeURIComponent(chapterId)}`,
      token, signal,
    ),

  getProjectContext: (token: string, projectId: string, limit = 25, signal?: AbortSignal) =>
    authenticatedGet<AdminContext>(
      `/admin/projects/${encodeURIComponent(projectId)}/context${query([['limit', limit]])}`,
      token, signal,
    ),

  listTasks: (token: string, filters: TaskFilters, signal?: AbortSignal) =>
    authenticatedGet<AdminPage<AdminTask>>(`/admin/tasks${query([
      ['user_id', filters.userId], ['project_id', filters.projectId], ['status', filters.status],
      ...paging(filters),
    ])}`, token, signal),

  getTask: (token: string, taskId: string, signal?: AbortSignal) =>
    authenticatedGet<AdminTaskDetail>(`/admin/tasks/${encodeURIComponent(taskId)}`, token, signal),

  listTaskRuns: (token: string, taskId: string, filters: PageFilters, signal?: AbortSignal) =>
    authenticatedGet<AdminPage<AdminRun>>(
      `/admin/tasks/${encodeURIComponent(taskId)}/runs${query(paging(filters))}`,
      token, signal,
    ),

  listRuns: (token: string, filters: RunFilters, signal?: AbortSignal) =>
    authenticatedGet<AdminPage<AdminRun>>(`/admin/runs${query([
      ['user_id', filters.userId], ['project_id', filters.projectId], ['node', filters.node],
      ...paging(filters),
    ])}`, token, signal),

  getRun: (token: string, runId: number, signal?: AbortSignal) =>
    authenticatedGet<AdminRunDetail>(`/admin/runs/${runId}`, token, signal),

  listAccessLogs: (token: string, filters: PageFilters, signal?: AbortSignal) =>
    authenticatedGet<AdminPage<AdminAccessLog>>(
      `/admin/access-logs${query(paging(filters))}`, token, signal,
    ),
}
