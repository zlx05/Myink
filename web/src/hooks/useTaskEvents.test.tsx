// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import { openSSE } from '../lib/sse'
import type { SSEEvent } from '../lib/sse'
import type { TaskDetail } from '../types'
import { useTaskEvents } from './useTaskEvents'

vi.mock('../lib/api', () => ({ api: { getTask: vi.fn() } }))
vi.mock('../lib/sse', () => ({ openSSE: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('loads archived runs from the task snapshot when the old SSE stream has expired', async () => {
  vi.mocked(openSSE).mockResolvedValue({ reason: 'expired' })
  const detail: TaskDetail = {
    task_id: 'old-task', task_type: 'chapter_generate', status: 'done', payload: { seq: 8 },
    error: null, retry_count: 0, trace_id: null, chapter_seq: 8, batch_task_id: null,
    created_at: null, cost_total: 0.01,
    runs: [{ task_id: 'old-task', node: 'write', model_id: 'stub', input_tokens: 10,
      output_tokens: 20, cache_hit: false, duration_ms: 100, cost_est: 0.01,
      retry_count: 0, degraded: false, error: null, detail: null }],
  }
  vi.mocked(api.getTask).mockResolvedValue(detail)

  const { result } = renderHook(() => useTaskEvents('old-task'))

  await waitFor(() => expect(result.current.phase).toBe('expired'))
  await waitFor(() => expect(result.current.runs.map((run) => run.node)).toEqual(['write']))
  expect(result.current.status).toBe('done')
  expect(api.getTask).toHaveBeenCalledWith('old-task')
})

it('loads the persisted snapshot immediately even when an old SSE stream stays open', async () => {
  vi.mocked(openSSE).mockImplementation((_url, _onEvent, opts) => new Promise((resolve) => {
    opts?.signal?.addEventListener('abort', () => resolve({ reason: 'aborted' }), { once: true })
  }))
  vi.mocked(api.getTask).mockResolvedValue({
    task_id: 'persisted-task', task_type: 'chapter_generate', status: 'done', payload: { seq: 6 },
    error: null, retry_count: 0, trace_id: null, chapter_seq: 6, batch_task_id: null,
    created_at: null, cost_total: 0.02,
    runs: [
      { task_id: 'persisted-task', node: 'write', model_id: 'stub', input_tokens: 10,
        output_tokens: 20, cache_hit: false, duration_ms: 100, cost_est: 0.01,
        retry_count: 0, degraded: false, error: null, detail: null },
      { task_id: 'persisted-task', node: 'audit', model_id: 'stub', input_tokens: 5,
        output_tokens: 5, cache_hit: false, duration_ms: 50, cost_est: 0.01,
        retry_count: 0, degraded: false, error: null,
        detail: { audit_verdict: { verdict: 'pass', reasons: [], findings: [], confidence: 1 } } },
    ],
  })

  const { result } = renderHook(() => useTaskEvents('persisted-task'))

  await waitFor(() => expect(result.current.runs.map((run) => run.node)).toEqual(['write', 'audit']))
  expect(result.current.status).toBe('done')
  expect(result.current.phase).toBe('connecting')
})

it('refreshes the run snapshot on every node event, not only at route/persist', async () => {
  const event = (patch: Partial<SSEEvent>): SSEEvent => ({
    type: 'node', task_id: 'flow-task', node: '', status: '', message: '',
    stage: '', chapter_seq: 0, attempt: 0, offset: 0,
    artifact_id: '', content: '', artifact: '', ...patch,
  })
  const running: TaskDetail = {
    task_id: 'flow-task', task_type: 'chapter_generate', status: 'running',
    payload: { seq: 4, mode: 'auto' }, error: null, retry_count: 0,
    trace_id: null, chapter_seq: 4, batch_task_id: null, created_at: null,
    cost_total: 0, runs: [],
  }
  const written: TaskDetail = {
    ...running,
    runs: [{ task_id: 'flow-task:ch4', node: 'write', model_id: 'stub', input_tokens: 1,
      output_tokens: 2, cache_hit: false, duration_ms: 10, cost_est: 0.001, retry_count: 0,
      degraded: false, error: null, detail: null }],
  }
  vi.mocked(api.getTask).mockResolvedValueOnce(running).mockResolvedValue(written)
  // write 节点落库（非 route/persist）：右栏耗时视图也要立刻跟上，否则会一直停在 plan
  vi.mocked(openSSE).mockImplementation((_url, onEvent, opts) => new Promise((resolve) => {
    onEvent(event({ node: 'write', task_id: 'flow-task:ch4' }))
    opts?.signal?.addEventListener('abort', () => resolve({ reason: 'aborted' }), { once: true })
  }))

  const { result } = renderHook(() => useTaskEvents('flow-task'))

  await waitFor(() => expect(result.current.runs.map((run) => run.node)).toEqual(['write']))
})

it('coalesces node-event snapshots instead of refetching on every node', async () => {
  const event = (patch: Partial<SSEEvent>): SSEEvent => ({
    type: 'node', task_id: 'burst-task', node: '', status: '', message: '',
    stage: '', chapter_seq: 0, attempt: 0, offset: 0,
    artifact_id: '', content: '', artifact: '', ...patch,
  })
  const base: TaskDetail = {
    task_id: 'burst-task', task_type: 'chapter_generate', status: 'running',
    payload: { seq: 2 }, error: null, retry_count: 0, trace_id: null,
    chapter_seq: 2, batch_task_id: null, created_at: null, cost_total: 0,
    runs: [{ task_id: 'burst-task', node: 'write', model_id: 'stub', input_tokens: 1,
      output_tokens: 2, cache_hit: false, duration_ms: 10, cost_est: 0.001,
      retry_count: 0, degraded: false, error: null, detail: null }],
  }
  vi.mocked(api.getTask).mockResolvedValue(base)
  // 一章十个节点密集落库：逐事件拉全量详情是 O(节点数) 次请求（批量下近似平方级）
  vi.mocked(openSSE).mockImplementation((_url, onEvent, opts) => new Promise((resolve) => {
    for (const node of ['load_state', 'recall', 'plan_chapter', 'write', 'route']) {
      onEvent(event({ node }))
    }
    opts?.signal?.addEventListener('abort', () => resolve({ reason: 'aborted' }), { once: true })
  }))

  const { result } = renderHook(() => useTaskEvents('burst-task'))

  await waitFor(() => expect(result.current.runs.map((run) => run.node)).toEqual(['write']))
  // 挂载快照 1 次 + 5 个节点合并为 1 次；逐事件各自拉会打到 6 次
  expect(vi.mocked(api.getTask).mock.calls.length).toBeLessThanOrEqual(2)
})

it('does not let a slow snapshot overwrite a newer SSE status', async () => {
  const base: TaskDetail = {
    task_id: 'race-task', task_type: 'chapter_generate', status: 'running',
    payload: { seq: 3 }, error: null, retry_count: 0, trace_id: null,
    chapter_seq: 3, batch_task_id: null, created_at: null, cost_total: 0, runs: [],
  }
  let resolveSlow!: (detail: TaskDetail) => void
  const slow = new Promise<TaskDetail>((resolve) => { resolveSlow = resolve })
  // 挂载快照（发起时是旧视图 running）尚未返回，SSE 已推送终态 done
  vi.mocked(api.getTask).mockReturnValueOnce(slow).mockResolvedValue({ ...base, status: 'done' })
  vi.mocked(openSSE).mockImplementation(async (_url, onEvent) => {
    onEvent({
      type: 'status', task_id: 'race-task', node: '', status: 'done', message: '',
      stage: '', chapter_seq: 0, attempt: 0, offset: 0,
      artifact_id: '', content: '', artifact: '',
    })
    return { reason: 'terminal' }
  })

  const { result } = renderHook(() => useTaskEvents('race-task'))

  await waitFor(() => expect(result.current.status).toBe('done'))
  await act(async () => { resolveSlow(base) })
  // 晚回的旧快照不得把右栏拽回 running（「左边已 write、右边还在 plan」的来源）
  expect(result.current.status).toBe('done')
})

it('assembles replayable artifact deltas and exposes the completed plan', async () => {
  const event = (patch: Partial<SSEEvent>): SSEEvent => ({
    type: 'artifact_delta', task_id: 'stream-task', node: '', status: '', message: '',
    stage: 'plan', chapter_seq: 17, attempt: 1, offset: 0,
    artifact_id: 'artifact-1', content: '', artifact: '', ...patch,
  })
  vi.mocked(api.getTask).mockResolvedValue({
    task_id: 'stream-task', task_type: 'chapter_generate', status: 'awaiting_plan',
    payload: { seq: 17, mode: 'manual' }, error: null, retry_count: 0,
    trace_id: null, chapter_seq: 17, batch_task_id: null, created_at: null,
    cost_total: 0, runs: [],
  })
  vi.mocked(openSSE).mockImplementation(async (_url, onEvent) => {
    onEvent(event({ type: 'artifact_reset' }))
    onEvent(event({ content: '甲🌙', offset: 0 }))
    onEvent(event({ content: '乙', offset: 2 }))
    onEvent(event({
      type: 'artifact_complete', offset: 3,
      artifact: JSON.stringify({ goals: ['推进'], expected_events: ['查证'] }),
    }))
    onEvent(event({ type: 'status', status: 'awaiting_plan' }))
    return { reason: 'terminal' }
  })

  const { result } = renderHook(() => useTaskEvents('stream-task'))
  await waitFor(() => expect(result.current.phase).toBe('terminal'))
  await waitFor(() => expect(result.current.artifacts).toHaveLength(1))
  expect(result.current.artifacts[0]).toMatchObject({
    content: '甲🌙乙', complete: true, attempt: 1,
    artifact: { goals: ['推进'], expected_events: ['查证'] },
  })
})

it('publishes an incomplete write artifact before the SSE request finishes', async () => {
  let releaseStream!: () => void
  const streamBlocked = new Promise<void>((resolve) => { releaseStream = resolve })
  const event = (patch: Partial<SSEEvent>): SSEEvent => ({
    type: 'artifact_delta', task_id: 'live-write-task', node: '', status: '', message: '',
    stage: 'write', chapter_seq: 8, attempt: 1, offset: 0,
    artifact_id: 'write-1', content: '', artifact: '', ...patch,
  })
  const runningDetail: TaskDetail = {
    task_id: 'live-write-task', task_type: 'chapter_generate', status: 'running',
    payload: { seq: 8, mode: 'auto' }, error: null, retry_count: 0,
    trace_id: null, chapter_seq: 8, batch_task_id: null, created_at: null,
    cost_total: 0, runs: [],
  }
  vi.mocked(api.getTask)
    .mockResolvedValueOnce(runningDetail)
    .mockResolvedValue({ ...runningDetail, status: 'done' })
  vi.mocked(openSSE).mockImplementation(async (_url, onEvent) => {
    onEvent(event({ type: 'artifact_reset' }))
    onEvent(event({ content: '模型仍在生成时到达的第一段', offset: 0 }))
    await streamBlocked
    onEvent(event({ type: 'artifact_complete', offset: 13 }))
    onEvent(event({ type: 'status', status: 'done' }))
    return { reason: 'terminal' }
  })

  const { result } = renderHook(() => useTaskEvents('live-write-task'))

  await waitFor(() => expect(result.current.artifacts[0]?.content).toBe('模型仍在生成时到达的第一段'))
  expect(result.current.artifacts[0].complete).toBe(false)
  expect(result.current.phase).not.toBe('terminal')

  await act(async () => { releaseStream() })
  await waitFor(() => expect(result.current.phase).toBe('terminal'))
})

it('keeps following a resumed task when SSE first replays an old review terminal', async () => {
  const base: TaskDetail = {
    task_id: 'resumed-task', task_type: 'chapter_generate', status: 'queued',
    payload: { seq: 14, mode: 'auto' }, error: null, retry_count: 0,
    trace_id: null, chapter_seq: 14, batch_task_id: null, created_at: null,
    cost_total: 0, runs: [],
  }
  // 挂载时的主动快照 + 历史终态后的权威快照都仍在续跑；第二次连接才到真正 done。
  vi.mocked(api.getTask)
    .mockResolvedValueOnce(base)
    .mockResolvedValueOnce(base)
    .mockResolvedValue({ ...base, status: 'done' })
  vi.mocked(openSSE)
    .mockImplementationOnce(async (_url, onEvent) => {
      onEvent({
        type: 'status', task_id: 'resumed-task', node: '', status: 'awaiting_review',
        message: '', stage: '', chapter_seq: 0, attempt: 0, offset: 0,
        artifact_id: '', content: '', artifact: '',
      })
      return { reason: 'terminal' }
    })
    .mockImplementationOnce(async (_url, onEvent) => {
      onEvent({
        type: 'status', task_id: 'resumed-task', node: '', status: 'done',
        message: '', stage: '', chapter_seq: 0, attempt: 0, offset: 0,
        artifact_id: '', content: '', artifact: '',
      })
      return { reason: 'terminal' }
    })

  const { result } = renderHook(() => useTaskEvents('resumed-task'))

  await waitFor(() => expect(openSSE).toHaveBeenCalledTimes(2), { timeout: 3000 })
  await waitFor(() => expect(result.current.phase).toBe('terminal'))
  expect(result.current.status).toBe('done')
  expect(api.getTask).toHaveBeenCalledTimes(3)
})

it('stops reconnecting and adopts the snapshot when the stream eofs on a finished task', async () => {
  // 流键仍存在（网关不会回 410）但 worker 已死：终态帧永不到达，网关 30min 硬超时后才 EOF。
  // 此前该分支只退避重连、从不查快照，phase 永远停在 reconnecting → 写按钮永久禁用。
  vi.mocked(api.getTask).mockResolvedValue({
    task_id: 'dead-worker-task', task_type: 'chapter_generate', status: 'done',
    payload: { seq: 9 }, error: null, retry_count: 0, trace_id: null,
    chapter_seq: 9, batch_task_id: null, created_at: null, cost_total: 0.01,
    runs: [{ task_id: 'dead-worker-task', node: 'write', model_id: 'stub', input_tokens: 10,
      output_tokens: 20, cache_hit: false, duration_ms: 100, cost_est: 0.01,
      retry_count: 0, degraded: false, error: null, detail: null }],
  })
  vi.mocked(openSSE).mockResolvedValue({ reason: 'eof' })

  const { result } = renderHook(() => useTaskEvents('dead-worker-task'))

  await waitFor(() => expect(result.current.phase).toBe('terminal'))
  expect(result.current.status).toBe('done')
  // 已完结任务的 run 历史必须仍然可回看，不能被这次收尾清掉
  await waitFor(() => expect(result.current.runs.map((run) => run.node)).toEqual(['write']))
})

it('keeps reconnecting when the stream expired but the task is still running', async () => {
  // 任务存活却 >1h 无事件 → 流键先被裁掉。不能就此置成 expired：'expired' 不在在途判定
  // 里，写按钮会在任务进行中被放出来，造成重复提交。
  vi.mocked(api.getTask).mockResolvedValue({
    task_id: 'slow-task', task_type: 'chapter_generate', status: 'running',
    payload: { seq: 11 }, error: null, retry_count: 0, trace_id: null,
    chapter_seq: 11, batch_task_id: null, created_at: null, cost_total: 0, runs: [],
  })
  vi.mocked(openSSE).mockResolvedValue({ reason: 'expired' })

  const { result } = renderHook(() => useTaskEvents('slow-task'))

  await waitFor(() => expect(result.current.phase).toBe('reconnecting'))
  await waitFor(() => expect(openSSE).toHaveBeenCalledTimes(2), { timeout: 3000 })
  expect(result.current.phase).toBe('reconnecting')
})
