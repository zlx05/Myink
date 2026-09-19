// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '../lib/api'
import { useAuth } from '../context/AuthContext'
import { adminApi, type AdminMetrics } from '../lib/adminApi'
import AdminPage from './AdminPage'

vi.mock('../context/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('../lib/adminApi', async (original) => {
  const actual = await original<typeof import('../lib/adminApi')>()
  return {
    ...actual,
    adminApi: Object.fromEntries(
      Object.keys(actual.adminApi).map((key) => [key, vi.fn()]),
    ) as unknown as typeof actual.adminApi,
  }
})

const metrics: AdminMetrics = {
  run_count: 2, input_tokens: 1200, output_tokens: 800, cost_est: 0.42, duration_ms: 3500,
}

const auth = {
  session: {
    token: 'token-admin', userId: 'admin-1', username: 'root', tier: 'normal',
    role: 'admin' as const, roleVerified: true, expiresAt: Date.now() + 60_000,
  },
  status: 'authenticated' as const,
  revalidate: vi.fn(),
  logout: vi.fn(),
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function renderPage() {
  return render(<MemoryRouter><AdminPage /></MemoryRouter>)
}

beforeEach(() => {
  vi.mocked(useAuth).mockReturnValue(auth as unknown as ReturnType<typeof useAuth>)
  auth.revalidate.mockReset()
  vi.mocked(adminApi.getOverview).mockResolvedValue({
    user_count: 4, project_count: 6, chapter_count: 18, task_count: 9,
    metrics, task_status_counts: { queued: 2, completed: 7 },
  })
  vi.mocked(adminApi.listUsers).mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
  vi.mocked(adminApi.listProjects).mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
  vi.mocked(adminApi.listTasks).mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
  vi.mocked(adminApi.listRuns).mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
  vi.mocked(adminApi.listAccessLogs).mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('shows the global overview and explains estimated metrics', async () => {
  renderPage()

  expect(await screen.findByRole('heading', { name: '管理员控制台' })).toBeTruthy()
  expect(await screen.findByText('4')).toBeTruthy()
  expect(screen.getByText('预估存储成本（非实际账单）')).toBeTruthy()
  expect(screen.getByText('节点计时合计；0 表示未记录')).toBeTruthy()
  expect(screen.getByText('queued')).toBeTruthy()
})

it('does not request or render protected data for a normal user', () => {
  vi.mocked(useAuth).mockReturnValue({
    ...auth,
    session: { ...auth.session, role: 'user', roleVerified: true },
  } as unknown as ReturnType<typeof useAuth>)
  renderPage()
  expect(screen.queryByText('管理员控制台')).toBeNull()
  expect(adminApi.getOverview).not.toHaveBeenCalled()
})

it('does not trust an authenticated cached admin whose role is not server-verified', () => {
  vi.mocked(useAuth).mockReturnValue({
    ...auth,
    status: 'authenticated',
    session: { ...auth.session, role: 'admin', roleVerified: false },
  } as unknown as ReturnType<typeof useAuth>)
  renderPage()
  expect(screen.queryByText('管理员控制台')).toBeNull()
  expect(adminApi.getOverview).not.toHaveBeenCalled()
})

it('discards an old account response after the token changes', async () => {
  const oldResult = deferred<Awaited<ReturnType<typeof adminApi.getOverview>>>()
  vi.mocked(adminApi.getOverview)
    .mockReturnValueOnce(oldResult.promise)
    .mockResolvedValueOnce({
      user_count: 2, project_count: 1, chapter_count: 3, task_count: 1,
      metrics: { ...metrics, run_count: 1 }, task_status_counts: {},
    })
  const view = renderPage()

  vi.mocked(useAuth).mockReturnValue({
    ...auth,
    session: { ...auth.session, token: 'token-admin-b', userId: 'admin-2' },
  } as unknown as ReturnType<typeof useAuth>)
  view.rerender(<MemoryRouter><AdminPage /></MemoryRouter>)
  expect(await screen.findByText('2')).toBeTruthy()

  oldResult.resolve({
    user_count: 99, project_count: 99, chapter_count: 99, task_count: 99,
    metrics, task_status_counts: {},
  })
  await Promise.resolve()
  expect(screen.queryByText('99')).toBeNull()
})

it('clears protected content and revalidates when an admin request returns 403', async () => {
  vi.mocked(adminApi.getOverview)
    .mockResolvedValueOnce({
      user_count: 4, project_count: 6, chapter_count: 18, task_count: 9,
      metrics, task_status_counts: {},
    })
    .mockRejectedValueOnce(new ApiError(403, 'FORBIDDEN', null))
  renderPage()
  expect(await screen.findByText('18')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '刷新当前视图' }))

  expect((await screen.findByRole('alert')).textContent).toContain('管理员权限已变化')
  expect(screen.queryByText('18')).toBeNull()
  expect(auth.revalidate).toHaveBeenCalledTimes(1)
})

it('searches users, opens their books and pages through results', async () => {
  vi.mocked(adminApi.listUsers)
    .mockResolvedValueOnce({
      items: [{
        id: 'user-1', username: 'alice', tier: 'normal', role: 'user', project_count: 2,
        chapter_count: 7, word_count: 12000, task_count: 3, metrics,
      }], total: 26, limit: 25, offset: 0,
    })
    .mockResolvedValueOnce({
      items: [{
        id: 'user-1', username: 'alice', tier: 'normal', role: 'user', project_count: 2,
        chapter_count: 7, word_count: 12000, task_count: 3, metrics,
      }], total: 26, limit: 25, offset: 0,
    })
    .mockResolvedValueOnce({
      items: [{
        id: 'user-1', username: 'alice', tier: 'normal', role: 'user', project_count: 2,
        chapter_count: 7, word_count: 12000, task_count: 3, metrics,
      }], total: 26, limit: 25, offset: 25,
    })
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '用户' }))
  expect(await screen.findByText('alice')).toBeTruthy()

  fireEvent.change(screen.getByLabelText('搜索用户'), { target: { value: 'ali ce' } })
  fireEvent.submit(screen.getByRole('search', { name: '用户筛选' }))
  await waitFor(() => expect(adminApi.listUsers).toHaveBeenLastCalledWith(
    'token-admin', expect.objectContaining({ q: 'ali ce', offset: 0 }), expect.any(AbortSignal),
  ))

  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  await waitFor(() => expect(adminApi.listUsers).toHaveBeenLastCalledWith(
    'token-admin', expect.objectContaining({ offset: 25 }), expect.any(AbortSignal),
  ))

  vi.mocked(adminApi.listProjects).mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
  fireEvent.click(screen.getByRole('button', { name: '查看 alice 的作品' }))
  await waitFor(() => expect(adminApi.listProjects).toHaveBeenCalledWith(
    'token-admin', expect.objectContaining({ userId: 'user-1' }), expect.any(AbortSignal),
  ))
})

it('loads chapter content and bounded project context only after selecting a book', async () => {
  vi.mocked(adminApi.listProjects).mockResolvedValue({
    items: [{
      id: 'book-1', user_id: 'user-1', username: 'alice', title: '山河册', genre: '奇幻',
      current_chapter: 2, target_words: 100000, creation_status: 'ready',
      created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z',
      chapter_count: 2, word_count: 4500, task_count: 1, metrics,
    }], total: 1, limit: 25, offset: 0,
  })
  vi.mocked(adminApi.listChapters).mockResolvedValue({
    items: [{
      id: 'chapter-1', project_id: 'book-1', chapter_seq: 1, title: '第一章', status: 'done',
      word_count: 2200, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z',
    }], total: 1, limit: 25, offset: 0,
  })
  vi.mocked(adminApi.getProjectContext).mockResolvedValue({
    project_id: 'book-1',
    settings: { data: { genre_pack: '东方奇幻' }, truncated: false, redacted: false, limits: { max_depth: 10, max_items: 80, max_text: 16000, max_bytes: 65536 } },
    outlines: { items: [], total: 0, limit: 25, truncated: false },
    events: { items: [], total: 0, limit: 25, truncated: false },
    facts: { items: [], total: 0, limit: 25, truncated: false },
    characters: { items: [], total: 0, limit: 25, truncated: false },
    foreshadows: { items: [], total: 0, limit: 25, truncated: false },
    threads: { items: [], total: 0, limit: 25, truncated: false },
  })
  vi.mocked(adminApi.getChapter).mockResolvedValue({
    id: 'chapter-1', project_id: 'book-1', chapter_seq: 1, title: '第一章', status: 'done',
    word_count: 2200, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z',
    content: '<script>不能执行</script>正文', summary: '章节摘要', version: 3,
  })
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '作品' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看《山河册》' }))
  expect((await screen.findByLabelText('作品设置')).textContent).toContain('东方奇幻')
  fireEvent.click(await screen.findByRole('button', { name: '查看第一章全文' }))
  expect(await screen.findByText('<script>不能执行</script>正文')).toBeTruthy()
  expect(screen.getByText('章节摘要')).toBeTruthy()
  expect(document.querySelector('script')).toBeNull()
})

it('shows task payload and ordered node detail, including legacy missing fields', async () => {
  vi.mocked(adminApi.listTasks).mockResolvedValue({
    items: [{
      id: 'task-1', project_id: 'book-1', user_id: 'user-1', username: 'alice', project_title: '山河册',
      task_type: 'generate', status: 'failed', chapter_seq: 1, batch_task_id: null, retry_count: 1,
      created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:01:00Z', metrics,
    }], total: 1, limit: 25, offset: 0,
  })
  vi.mocked(adminApi.getTask).mockResolvedValue({
    id: 'task-1', project_id: 'book-1', user_id: 'user-1', username: 'alice', project_title: '山河册',
    task_type: 'generate', status: 'failed', chapter_seq: 1, batch_task_id: null, retry_count: 1,
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:01:00Z', metrics,
    payload: { data: { chapter: 1 }, truncated: false, redacted: false, limits: { max_depth: 10, max_items: 80, max_text: 16000, max_bytes: 65536 } },
    error: { data: '模型超时', truncated: false, redacted: false, limits: { max_depth: 10, max_items: 80, max_text: 16000, max_bytes: 65536 } },
    elapsed_ms: 60000, elapsed_includes_waits: true,
  })
  vi.mocked(adminApi.listTaskRuns).mockResolvedValue({
    items: [{
      id: 11, project_id: 'book-1', user_id: 'user-1', username: 'alice', project_title: '山河册',
      task_id: 'task-1', node: 'draft', role: 'writer', model_id: 'model-a', input_tokens: 12,
      output_tokens: 24, cost_est: 0.1, duration_ms: 0, cache_hit: false, degraded: true,
      retry_count: 1, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:01Z',
    }], total: 1, limit: 25, offset: 0,
  })
  vi.mocked(adminApi.getRun).mockResolvedValue({
    id: 11, project_id: 'book-1', user_id: 'user-1', username: 'alice', project_title: '山河册',
    task_id: 'task-1', node: 'draft', role: 'writer', model_id: 'model-a', input_tokens: 12,
    output_tokens: 24, cost_est: 0.1, duration_ms: 0, cache_hit: false, degraded: true,
    retry_count: 1, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:01Z',
    detail: { data: null, truncated: false, redacted: false, limits: { max_depth: 10, max_items: 80, max_text: 16000, max_bytes: 65536 } },
    error: { data: '旧错误', truncated: true, redacted: true, limits: { max_depth: 10, max_items: 80, max_text: 16000, max_bytes: 65536 } },
    detail_missing: true, prompt_missing: true,
  })
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '任务' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看任务 task-1' }))
  expect(await screen.findByText('模型超时')).toBeTruthy()
  expect(screen.getByText('包含排队、暂停与人工等待')).toBeTruthy()
  fireEvent.click(await screen.findByRole('button', { name: '查看节点 draft #11' }))
  expect(await screen.findByText('旧记录未保存详情，无法重建。')).toBeTruthy()
  expect(screen.getByText('旧记录未保存提示词，无法重建。')).toBeTruthy()
  expect(screen.getByText('内容已截断')).toBeTruthy()
  expect(screen.getByText('敏感信息已脱敏')).toBeTruthy()
  expect(screen.getByText('0 表示未记录')).toBeTruthy()
})

it('uses the refreshed task detail status instead of the selected list snapshot', async () => {
  const task = {
    id: 'task-refresh', project_id: 'book-1', user_id: 'user-1', username: 'alice', project_title: '山河册',
    task_type: 'generate', status: 'queued', chapter_seq: 1, batch_task_id: null, retry_count: 0,
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', metrics,
  }
  const capture = {
    data: null, truncated: false, redacted: false,
    limits: { max_depth: 10, max_items: 80, max_text: 16000, max_bytes: 65536 },
  }
  vi.mocked(adminApi.listTasks).mockResolvedValue({ items: [task], total: 1, limit: 25, offset: 0 })
  vi.mocked(adminApi.listTaskRuns).mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
  vi.mocked(adminApi.getTask)
    .mockResolvedValueOnce({ ...task, payload: capture, error: capture, elapsed_ms: 0, elapsed_includes_waits: true })
    .mockResolvedValueOnce({
      ...task, status: 'done', updated_at: '2026-01-01T00:01:00Z',
      payload: capture, error: capture, elapsed_ms: 60000, elapsed_includes_waits: true,
    })
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '任务' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看任务 task-refresh' }))
  const heading = await screen.findByRole('heading', { name: '任务 task-refresh' })
  const detailSection = heading.closest('section')
  expect(detailSection).not.toBeNull()
  expect(await screen.findByText('山河册 · generate · queued')).toBeTruthy()

  fireEvent.click(within(detailSection as HTMLElement).getByRole('button', { name: '刷新当前视图' }))

  expect(await screen.findByText('山河册 · generate · done')).toBeTruthy()
})

it('keeps taskless setup runs discoverable and loads access logs', async () => {
  vi.mocked(adminApi.listRuns).mockResolvedValue({
    items: [{
      id: 20, project_id: 'book-1', user_id: 'user-1', username: 'alice', project_title: '山河册',
      task_id: null, node: 'book_setup', role: null, model_id: 'model-a', input_tokens: 10,
      output_tokens: 20, cost_est: 0.03, duration_ms: 1200, cache_hit: false, degraded: false,
      retry_count: 0, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:01Z',
    }], total: 1, limit: 25, offset: 0,
  })
  vi.mocked(adminApi.listAccessLogs).mockResolvedValue({
    items: [{ id: 2, actor_id: 'admin-1', action: 'admin.overview', target: 'collection', created_at: '2026-01-01T00:00:00Z' }],
    total: 1, limit: 25, offset: 0,
  })
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '全部运行' }))
  expect(await screen.findByText('无关联任务')).toBeTruthy()
  expect(await screen.findByText(/未降级/)).toBeTruthy()
  expect(screen.getByRole('button', { name: '查看节点 book_setup #20' })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '访问日志' }))
  expect(await screen.findByText('admin.overview')).toBeTruthy()
  expect(screen.getByText('collection')).toBeTruthy()
})
