// SSE 帧解析器单测：帧缓冲切块 / 行解析 / 注释行跳过 / 坏 data 跳帧 / 终态判定 / openSSE 流行为。
// node 环境（token 模块 mock 掉，无需 jsdom）。
import { dispatchUnauthorized } from './token'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const tokenState = vi.hoisted(() => ({ value: 'test-token' }))

vi.mock('../lib/token', () => ({
  getToken: () => tokenState.value,
  dispatchUnauthorized: vi.fn(),
}))

import {
  isTerminalStatus,
  openSSE,
  parseData,
  parseSSEFrame,
  takeFrames,
  type SSEEvent,
} from './sse'

describe('takeFrames 缓冲切块', () => {
  it('空缓冲无帧', () => {
    expect(takeFrames('')).toEqual({ frames: [], rest: '' })
  })

  it('单帧按 \\n\\n 切出，尾部残段留缓冲', () => {
    const { frames, rest } = takeFrames('event: node\n\ndata: x')
    expect(frames).toEqual(['event: node'])
    expect(rest).toBe('data: x')
  })

  it('多帧依次切出', () => {
    const { frames, rest } = takeFrames('a\n\nb\n\nc')
    expect(frames).toEqual(['a', 'b'])
    expect(rest).toBe('c')
  })
})

describe('parseSSEFrame 行解析', () => {
  it('完整帧切出 event/id/data，data 多行以换行连接', () => {
    const f = parseSSEFrame(
      'event: node\r\nid: 1720000000000-0\r\ndata: {"type":"node"}\r\ndata: 第二行',
    )
    expect(f).toEqual({
      event: 'node',
      id: '1720000000000-0',
      data: '{"type":"node"}\n第二行',
    })
  })

  it('注释帧（心跳）与未知字段忽略，无 data 时 data 为 null', () => {
    const f = parseSSEFrame(': keepalive\nX-custom: v')
    expect(f).toEqual({ event: null, id: null, data: null })
  })
})

describe('parseData 载荷解析', () => {
  it('合法 JSON 归一为 SSEEvent', () => {
    expect(
      parseData('{"type":"node","task_id":"t1","node":"write","status":"","message":"m"}'),
    ).toEqual({
      type: 'node', task_id: 't1', node: 'write', status: '', message: 'm',
      stage: '', chapter_seq: 0, attempt: 0, offset: 0,
      artifact_id: '', content: '', artifact: '',
    })
  })

  it('坏 JSON / 缺 type 返回 null（跳帧容错）', () => {
    expect(parseData('not-json')).toBeNull()
    expect(parseData('{"node":"write"}')).toBeNull()
  })
})

describe('isTerminalStatus 终态判定', () => {
  it('done/failed/awaiting_plan/awaiting_review/cancelled 为终态', () => {
    expect(isTerminalStatus('done')).toBe(true)
    expect(isTerminalStatus('failed')).toBe(true)
    expect(isTerminalStatus('awaiting_plan')).toBe(true)
    expect(isTerminalStatus('awaiting_review')).toBe(true)
    expect(isTerminalStatus('cancelled')).toBe(true)
  })

  it('运行态/空值非终态', () => {
    expect(isTerminalStatus('running')).toBe(false)
    expect(isTerminalStatus('queued')).toBe(false)
    expect(isTerminalStatus(null)).toBe(false)
    expect(isTerminalStatus(undefined)).toBe(false)
  })
})

describe('openSSE 流行为', () => {
  const originalFetch = globalThis.fetch
  const mockDispatch = vi.mocked(dispatchUnauthorized)

  beforeEach(() => {
    tokenState.value = 'test-token'
    mockDispatch.mockClear()
  })
  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  function streamOf(...chunks: string[]): Response {
    const enc = new TextEncoder()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(enc.encode(c))
        controller.close()
      },
    })
    return new Response(body, { status: 200 })
  }

  it('实时帧推进 onEvent 与 onLastId；终态后返回 terminal', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      streamOf(
        'event: node\nid: 1-0\ndata: {"type":"node","task_id":"t1","node":"write"}\n\n',
        'event: status\nid: 2-0\ndata: {"type":"status","status":"done"}\n\n',
      ),
    )
    const events: SSEEvent[] = []
    const lastIds: string[] = []
    const result = await openSSE('/api/v1/tasks/t1/events', (ev) => events.push(ev), {
      onLastId: (id) => lastIds.push(id),
    })
    expect(result.reason).toBe('terminal')
    expect(events).toHaveLength(2)
    expect(events[0].node).toBe('write')
    expect(lastIds).toEqual(['1-0', '2-0'])
  })

  it('心跳注释帧与坏 data 帧被跳过，不影响后续帧', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      streamOf(
        ': keepalive\n\n',
        'event: node\nid: 1-0\ndata: not-json\n\n',
        'event: status\nid: 2-0\ndata: {"type":"status","status":"failed"}\n\n',
      ),
    )
    const events: SSEEvent[] = []
    const result = await openSSE('/x', (ev) => events.push(ev))
    expect(result.reason).toBe('terminal')
    expect(events).toHaveLength(1)
    expect(events[0].status).toBe('failed')
  })

  it('流 EOF 未达终态 → eof（可重连）', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(streamOf('event: node\nid: 1-0\ndata: {"type":"node"}\n\n'))
    const result = await openSSE('/x', () => {})
    expect(result.reason).toBe('eof')
  })

  it('410 → expired（回退 GET 快照）', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ error: 'sse_stream_expired', hint: 'fallback_get_snapshot' }),
          { status: 410 },
        ),
      )
    const result = await openSSE('/x', () => {})
    expect(result.reason).toBe('expired')
    expect(result.status).toBe(410)
  })

  it('401 → unauthorized 并派发登出事件', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response('', { status: 401 }))
    const result = await openSSE('/x', () => {})
    expect(result.reason).toBe('unauthorized')
    expect(mockDispatch).toHaveBeenCalledWith('test-token')
  })

  it('discards a delayed 401 from the previous token without signing out the new account', async () => {
    let resolveFetch!: (response: Response) => void
    globalThis.fetch = vi.fn().mockReturnValue(new Promise((resolve) => { resolveFetch = resolve }))

    const pending = openSSE('/x', () => {})
    tokenState.value = 'new-token'
    resolveFetch(new Response('', { status: 401 }))

    await expect(pending).resolves.toEqual({ reason: 'aborted' })
    expect(mockDispatch).not.toHaveBeenCalled()
  })

  it('last_event_id 参数拼接进 URL（查询参数追加）', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(streamOf(''))
    await openSSE('/api/v1/tasks/t1/events?foo=1', () => {}, {
      lastEventId: 'abc',
    })
    const called = vi.mocked(globalThis.fetch).mock.calls[0][0]
    expect(called).toBe('/api/v1/tasks/t1/events?foo=1&last_event_id=abc')
    const headers = vi.mocked(globalThis.fetch).mock.calls[0][1]?.headers as Record<
      string,
      string
    >
    expect(headers.Authorization).toBe('Bearer test-token')
  })

  it('fetch 网络失败 → network', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('down'))
    const result = await openSSE('/x', () => {})
    expect(result.reason).toBe('network')
  })
})
