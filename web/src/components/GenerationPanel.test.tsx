// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from '../lib/api'
import type { ChapterMeta } from '../types'
import { GenerationPanel } from './GenerationPanel'

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      generateChapter: vi.fn(),
      generateBatch: vi.fn(),
    },
  }
})

const chapter: ChapterMeta = {
  id: 'chapter-16',
  chapter_seq: 16,
  title: '第十六章',
  status: 'confirmed',
  word_count: 3000,
  summary: null,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('shows the writing controls by default and starts the next chapter from a direct click', async () => {
  vi.mocked(api.generateChapter).mockResolvedValue({ task_id: 'task-17', trace_id: 'trace-17', status: 'queued' })
  const onTaskStart = vi.fn()

  render(
    <GenerationPanel
      projectId="project-1"
      chapters={[chapter]}
      selectedChapter={chapter}
      onTaskStart={onTaskStart}
    />,
  )

  const button = screen.getByRole('button', { name: '写下一章（第 17 章）' })
  expect(button).toBeTruthy()
  fireEvent.click(button)

  await waitFor(() => expect(api.generateChapter).toHaveBeenCalledWith(
    'project-1',
    'chapter-16',
    { seq: 17, mode: 'auto' },
  ))
  expect(onTaskStart).toHaveBeenCalledWith('task-17', undefined, 17, 'auto')
  expect((await screen.findByRole('status')).textContent).toContain('第 17 章写作任务已创建')
})

it('passes the optional writing instruction and exposes a request error', async () => {
  vi.mocked(api.generateChapter).mockRejectedValue(new ApiError(429, 'CONCURRENCY_LIMIT', null))

  render(
    <GenerationPanel
      projectId="project-1"
      chapters={[chapter]}
      selectedChapter={chapter}
      onTaskStart={() => {}}
    />,
  )

  fireEvent.change(screen.getByLabelText('写作指令（可留空）'), { target: { value: '承接上一章结尾' } })
  fireEvent.click(screen.getByRole('button', { name: '写下一章（第 17 章）' }))

  await waitFor(() => expect(api.generateChapter).toHaveBeenCalledWith(
    'project-1',
    'chapter-16',
    { seq: 17, mode: 'auto', user_instruction: '承接上一章结尾' },
  ))
  expect((await screen.findByRole('alert')).textContent).toContain('本书已有进行中的任务')
})

it('continues a failed empty placeholder instead of skipping to another chapter', async () => {
  vi.mocked(api.generateChapter).mockResolvedValue({ task_id: 'task-17b', trace_id: 'trace-17b', status: 'queued' })
  const placeholder: ChapterMeta = {
    id: 'chapter-17-writing',
    chapter_seq: 17,
    title: null,
    status: 'failed',
    word_count: 0,
    summary: null,
  }

  render(
    <GenerationPanel
      projectId="project-1"
      chapters={[chapter, placeholder]}
      selectedChapter={placeholder}
      onTaskStart={() => {}}
    />,
  )

  fireEvent.click(screen.getByRole('button', { name: '继续生成第 17 章' }))
  await waitFor(() => expect(api.generateChapter).toHaveBeenCalledWith(
    'project-1',
    'chapter-17-writing',
    { seq: 17, mode: 'auto' },
  ))
})

it('shows the target chapter as running and prevents a duplicate start', () => {
  const writing: ChapterMeta = {
    id: 'chapter-17-writing',
    chapter_seq: 17,
    title: null,
    status: 'writing',
    word_count: 0,
    summary: null,
  }

  render(
    <GenerationPanel
      projectId="project-1"
      chapters={[chapter, writing]}
      selectedChapter={writing}
      onTaskStart={() => {}}
    />,
  )

  expect(screen.getByText('当前生成：第 17 章')).toBeTruthy()
  expect((screen.getByRole('button', { name: '第 17 章生成中…' }) as HTMLButtonElement).disabled).toBe(true)
})

it('binds a batch task to its first chapter page', async () => {
  vi.mocked(api.generateBatch).mockResolvedValue({ task_id: 'batch-17', trace_id: 'trace-batch', status: 'queued' })
  const onTaskStart = vi.fn()

  render(
    <GenerationPanel
      projectId="project-1"
      chapters={[chapter]}
      selectedChapter={chapter}
      onTaskStart={onTaskStart}
    />,
  )

  fireEvent.click(screen.getByRole('button', { name: '发起批次' }))
  await waitFor(() => expect(api.generateBatch).toHaveBeenCalledWith(
    'project-1',
    { size: 3, start: 17 },
  ))
  expect(onTaskStart).toHaveBeenCalledWith('batch-17', 3, 17)
})

it('clears the previous book notice when switching projects', async () => {
  vi.mocked(api.generateChapter).mockResolvedValue({ task_id: 'task-17', trace_id: 'trace-17', status: 'queued' })
  const { rerender } = render(
    <GenerationPanel
      projectId="project-a"
      chapters={[chapter]}
      selectedChapter={chapter}
      onTaskStart={() => {}}
    />,
  )
  fireEvent.click(screen.getByRole('button', { name: '写下一章（第 17 章）' }))
  expect((await screen.findByRole('status')).textContent).toContain('第 17 章写作任务已创建')

  rerender(
    <GenerationPanel
      projectId="project-b"
      chapters={[]}
      selectedChapter={null}
      onTaskStart={() => {}}
    />,
  )
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.getByText('下一章：第 1 章')).toBeTruthy()
})

it('manual mode is sent with the chapter request and disables batch generation', async () => {
  vi.mocked(api.generateChapter).mockResolvedValue({ task_id: 'manual-17', trace_id: 'trace-manual', status: 'queued' })
  const onTaskStart = vi.fn()

  render(
    <GenerationPanel
      projectId="project-1"
      chapters={[chapter]}
      selectedChapter={chapter}
      onTaskStart={onTaskStart}
    />,
  )

  fireEvent.click(screen.getByRole('button', { name: '手动确认 Plan' }))
  expect((screen.getByLabelText('批次生成') as HTMLInputElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: '发起批次' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText('手动模式需要逐章确认计划，批量生成已禁用。')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '写下一章（第 17 章）' }))
  await waitFor(() => expect(api.generateChapter).toHaveBeenCalledWith(
    'project-1',
    'chapter-16',
    { seq: 17, mode: 'manual' },
  ))
  expect(onTaskStart).toHaveBeenCalledWith('manual-17', undefined, 17, 'manual')
})
