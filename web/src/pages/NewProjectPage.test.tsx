// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import NewProjectPage from './NewProjectPage'
import { api } from '../lib/api'
import type { OutlineDraft } from '../types'

vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ logout: vi.fn() }) }))
vi.mock('../components/RankingsPanel', () => ({ RankingsPanel: () => null }))
vi.mock('../components/ProjectRail', () => ({ ProjectRail: () => null }))
vi.mock('../lib/api', async (original) => ({
  ...await original<typeof import('../lib/api')>(),
  api: { listGenrePacks: vi.fn().mockResolvedValue([]), listProjects: vi.fn().mockResolvedValue([]),
    getCreation: vi.fn(), createProject: vi.fn(), setupDraft: vi.fn(), outlineDraft: vi.fn(),
    updateProject: vi.fn(), confirmSetup: vi.fn(), confirmOutline: vi.fn() },
}))
afterEach(() => {
  cleanup()
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

function NavigationControls() {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => navigate('/projects/new')}>new-project</button>
      <button type="button" onClick={() => navigate('/projects/new?draft=draft-b')}>draft-b</button>
    </>
  )
}

function renderPage(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/projects/new" element={<><NewProjectPage /><NavigationControls /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

function fillCreationBrief() {
  fireEvent.change(screen.getByLabelText('创作简报'), { target: { value: '寻找真相' } })
}

it('restores an unfinished book without creating a duplicate or exposing a skip button', async () => {
  vi.mocked(api.getCreation).mockResolvedValue({
    project: { id: 'draft-id', title: '我的草稿', genre: '悬疑', current_chapter: 0,
      target_words: 3000, creation_status: 'setup_confirmed' },
    context: { premise: '尚未写完的创作简报', chapter_count: 50,
      setup_draft: { world_rules: { rule: '规则' } } },
  })
  render(<MemoryRouter initialEntries={['/projects/new?draft=draft-id']}><NewProjectPage /></MemoryRouter>)
  expect(await screen.findByDisplayValue('我的草稿')).toBeTruthy()
  expect(screen.getByDisplayValue('尚未写完的创作简报')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /暂不规划/ })).toBeNull()
  expect((screen.getByRole('button', { name: '作品草稿已保存' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.createProject).not.toHaveBeenCalled()
})

it('clears the restored draft when the same component navigates to a fresh form', async () => {
  vi.mocked(api.getCreation).mockResolvedValue({
    project: { id: 'draft-a', title: 'A 书名', genre: '悬疑', current_chapter: 0,
      target_words: 3000, creation_status: 'draft' },
    context: { premise: 'A 简报', chapter_count: 50,
      setup_draft: { world_rules: { rule: 'A 规则' } } },
  })
  renderPage('/projects/new?draft=draft-a')
  expect(await screen.findByDisplayValue('A 书名')).toBeTruthy()
  expect(screen.getByRole('heading', { name: '② 设定骨架草稿' })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: 'new-project' }))

  await waitFor(() => expect((screen.getByLabelText('书名') as HTMLInputElement).value).toBe(''))
  expect(screen.getByRole('heading', { name: '新建作品' })).toBeTruthy()
  expect(screen.queryByRole('heading', { name: '② 设定骨架草稿' })).toBeNull()
  expect((screen.getByRole('button', { name: '创建作品' }) as HTMLButtonElement).disabled).toBe(false)
})

it('clears draft A and keeps the form locked when restoring draft B fails', async () => {
  vi.mocked(api.getCreation).mockImplementation(async (draftId: string) => {
    if (draftId === 'draft-b') throw new Error('B restore failed')
    return {
      project: { id: 'draft-a', title: 'A 书名', genre: '悬疑', current_chapter: 0,
        target_words: 3000, creation_status: 'draft' },
      context: { premise: 'A 简报', chapter_count: 50,
        setup_draft: { world_rules: { rule: 'A 规则' } } },
    }
  })
  renderPage('/projects/new?draft=draft-a')
  expect(await screen.findByDisplayValue('A 书名')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: 'draft-b' }))

  expect(await screen.findByText('恢复草稿失败，请刷新重试')).toBeTruthy()
  expect((screen.getByLabelText('书名') as HTMLInputElement).value).toBe('')
  expect(screen.queryByRole('heading', { name: '② 设定骨架草稿' })).toBeNull()
  expect((screen.getByRole('button', { name: '创建作品' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.getCreation).toHaveBeenNthCalledWith(1, 'draft-a')
  expect(api.getCreation).toHaveBeenNthCalledWith(2, 'draft-b')
})

it('does not let a late draft A response overwrite restored draft B', async () => {
  let resolveA!: (value: Awaited<ReturnType<typeof api.getCreation>>) => void
  vi.mocked(api.getCreation).mockImplementation((draftId: string) => {
    if (draftId === 'draft-a') return new Promise((resolve) => { resolveA = resolve })
    return Promise.resolve({
      project: { id: 'draft-b', title: 'B 书名', genre: '悬疑', current_chapter: 0,
        target_words: 3000, creation_status: 'draft' },
      context: { premise: 'B 简报', chapter_count: 80, setup_draft: {} },
    })
  })
  renderPage('/projects/new?draft=draft-a')
  await waitFor(() => expect(api.getCreation).toHaveBeenCalledWith('draft-a'))

  fireEvent.click(screen.getByRole('button', { name: 'draft-b' }))
  expect(await screen.findByDisplayValue('B 书名')).toBeTruthy()
  await act(async () => resolveA({
    project: { id: 'draft-a', title: 'A 书名', genre: '悬疑', current_chapter: 0,
      target_words: 3000, creation_status: 'draft' },
    context: { premise: 'A 简报', chapter_count: 50, setup_draft: {} },
  }))

  expect((screen.getByLabelText('书名') as HTMLInputElement).value).toBe('B 书名')
  expect((screen.getByLabelText('创作简报') as HTMLTextAreaElement).value).toBe('B 简报')
})

it('keeps creation locked until both proposals finish and never claims degraded generation succeeded', async () => {
  let finishOutline!: (value: OutlineDraft) => void
  vi.mocked(api.createProject).mockResolvedValue({ id: 'new-id', title: '书', genre: '悬疑', current_chapter: 0, target_words: 3000, creation_status: 'draft' })
  vi.mocked(api.setupDraft).mockResolvedValue({ draft: {}, error: '模型不可用' })
  vi.mocked(api.outlineDraft).mockReturnValue(new Promise((resolve) => { finishOutline = resolve }))
  render(<MemoryRouter><NewProjectPage /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('创作简报'), { target: { value: '寻找真相' } })
  fireEvent.click(screen.getByRole('button', { name: '创建作品' }))
  await waitFor(() => expect(api.outlineDraft).toHaveBeenCalledTimes(1))
  expect((screen.getByRole('button', { name: /作品草稿已保存/ }) as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: /重新生成草稿/ }) as HTMLButtonElement).disabled).toBe(true)
  finishOutline({ outline: { objective: '', volumes: [] }, error: '模型不可用' })
  expect(await screen.findByText(/草稿生成未全部完成/)).toBeTruthy()
  expect(screen.queryByText('作品已创建，设定与整书大纲草稿已生成，可编辑后确认')).toBeNull()
  expect(vi.mocked(api.createProject).mock.calls[0][0].request_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  )
  expect(sessionStorage.getItem('myink.pending-project-creation')).toBeNull()
})

it('reuses one persisted request id when the first create attempt is retried', async () => {
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => '11111111-1111-4111-8111-111111111111') })
  vi.mocked(api.createProject).mockRejectedValue(new Error('temporary failure'))
  render(<MemoryRouter><NewProjectPage /></MemoryRouter>)
  fillCreationBrief()

  fireEvent.click(screen.getByRole('button', { name: '创建作品' }))
  expect(await screen.findByText('创建作品失败，请重试')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '创建作品' }))
  await waitFor(() => expect(api.createProject).toHaveBeenCalledTimes(2))

  expect(vi.mocked(api.createProject).mock.calls.map(([body]) => body.request_id)).toEqual([
    '11111111-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
  ])
  expect(sessionStorage.getItem('myink.pending-project-creation')).toBe(
    '11111111-1111-4111-8111-111111111111',
  )
})

it('reuses the persisted request id after the page remounts', async () => {
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => '22222222-2222-4222-8222-222222222222') })
  vi.mocked(api.createProject).mockRejectedValue(new Error('temporary failure'))
  const first = render(<MemoryRouter><NewProjectPage /></MemoryRouter>)
  fillCreationBrief()
  fireEvent.click(screen.getByRole('button', { name: '创建作品' }))
  expect(await screen.findByText('创建作品失败，请重试')).toBeTruthy()

  first.unmount()
  render(<MemoryRouter><NewProjectPage /></MemoryRouter>)
  fillCreationBrief()
  fireEvent.click(screen.getByRole('button', { name: '创建作品' }))
  await waitFor(() => expect(api.createProject).toHaveBeenCalledTimes(2))

  expect(vi.mocked(api.createProject).mock.calls.map(([body]) => body.request_id)).toEqual([
    '22222222-2222-4222-8222-222222222222',
    '22222222-2222-4222-8222-222222222222',
  ])
})

it.each(['getItem', 'setItem'] as const)(
  'does not create without a safely persisted request id when sessionStorage.%s fails',
  async (method) => {
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => '33333333-3333-4333-8333-333333333333') })
    vi.spyOn(Storage.prototype, method).mockImplementation(() => {
      throw new DOMException('storage unavailable')
    })
    render(<MemoryRouter><NewProjectPage /></MemoryRouter>)
    fillCreationBrief()
    fireEvent.click(screen.getByRole('button', { name: '创建作品' }))

    expect(await screen.findByText('无法安全保存创建请求，请检查浏览器存储权限后重试')).toBeTruthy()
    expect(api.createProject).not.toHaveBeenCalled()
  },
)
