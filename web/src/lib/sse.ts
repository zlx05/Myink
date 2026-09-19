// fetch 版 SSE 客户端（阶段 4 前端核心难点）。
// 原生 EventSource 无法带 Authorization 头（网关 SSE 一律 Bearer），故用
// fetch + ReadableStream 解析 SSE 帧。网关帧格式（sse/stream.go:142）：
//   event: status|node      （type=message 时省略 event 行）
//   id: <redis-stream-id>   （断线重连 last_event_id 追平用）
//   data: {"type","task_id","node","status","message"}
//   （空行终结；心跳为注释帧 ": keepalive"）
// 终态 status∈{done,failed,awaiting_review,cancelled} 后网关关流；
// 流过期（Redis key 被裁剪）网关回 410 JSON → 前端回退 GET /tasks/:id 快照。

import { dispatchUnauthorized, getToken } from './token'

/** 单帧纯解析：切成 event/id/data 字段；注释行（心跳）忽略 */
export interface ParsedSSEFrame {
  event: string | null
  id: string | null
  data: string | null
}

export function parseSSEFrame(frame: string): ParsedSSEFrame {
  let event: string | null = null
  let id: string | null = null
  const dataLines: string[] = []
  for (const raw of frame.replace(/\r/g, '').split('\n')) {
    if (raw === '' || raw.startsWith(':')) continue // 注释帧
    if (raw.startsWith('event:')) {
      event = raw.slice(6).trim()
      continue
    }
    if (raw.startsWith('id:')) {
      id = raw.slice(3).trim()
      continue
    }
    if (raw.startsWith('data:')) {
      let v = raw.slice(5)
      if (v.startsWith(' ')) v = v.slice(1) // SSE 规范：字段值剥离单个前导空格
      dataLines.push(v)
      continue
    }
    // 未知字段忽略
  }
  return { event, id, data: dataLines.length > 0 ? dataLines.join('\n') : null }
}

/** 从累积缓冲提取完整帧（以 \n\n 终结），返回帧数组与剩余缓冲 */
export function takeFrames(buf: string): { frames: string[]; rest: string } {
  const frames: string[] = []
  let rest = buf
  let idx: number
  while ((idx = rest.indexOf('\n\n')) !== -1) {
    frames.push(rest.slice(0, idx))
    rest = rest.slice(idx + 2)
  }
  return { frames, rest }
}

/** data 载荷：网关 SSE 事件 JSON → 结构体；坏 JSON 返回 null（跳帧容错） */
export interface SSEEvent {
  type: string
  task_id: string
  node: string
  status: string
  message: string
  stage: string
  chapter_seq: number
  attempt: number
  offset: number
  artifact_id: string
  content: string
  artifact: string
}

export function parseData(data: string): SSEEvent | null {
  try {
    const obj = JSON.parse(data) as Partial<SSEEvent>
    if (typeof obj.type !== 'string') return null
    return {
      type: obj.type,
      task_id: obj.task_id ?? '',
      node: obj.node ?? '',
      status: obj.status ?? '',
      message: obj.message ?? '',
      stage: obj.stage ?? '',
      chapter_seq: typeof obj.chapter_seq === 'number' ? obj.chapter_seq : 0,
      attempt: typeof obj.attempt === 'number' ? obj.attempt : 0,
      offset: typeof obj.offset === 'number' ? obj.offset : 0,
      artifact_id: obj.artifact_id ?? '',
      content: obj.content ?? '',
      artifact: obj.artifact ?? '',
    }
  } catch {
    return null
  }
}

/** 终态判定（网关收到终态 status 后关流） */
export function isTerminalStatus(status: string | undefined | null): boolean {
  return (
    status === 'done' ||
    status === 'failed' ||
    status === 'awaiting_plan' ||
    status === 'awaiting_review' ||
    status === 'cancelled'
  )
}

export type SSECloseReason =
  | 'terminal' // 收到终态 status 事件后流正常结束
  | 'eof' // 流结束但未达终态（网络中断/网关 30min 硬超时）→ 可重连
  | 'aborted' // AbortSignal 触发（用户 stop / taskId 切换）
  | 'expired' // 410：流过期 → 调用方回退 GET 快照
  | 'unauthorized' // 401 → 登出
  | 'error' // 其他非 2xx
  | 'network' // fetch 网络失败

export interface SSEResult {
  reason: SSECloseReason
  status?: number
  body?: unknown
}

export interface OpenSSEOptions {
  lastEventId?: string
  signal?: AbortSignal
  /** 每次收到带 id 的帧回调（hook 记录断线重连游标） */
  onLastId?: (id: string) => void
}

/** 打开 SSE 流：返回的 Promise 在流结束时 resolve 关闭原因 */
export function openSSE(
  url: string,
  onEvent: (ev: SSEEvent) => void,
  opts?: OpenSSEOptions,
): Promise<SSEResult> {
  const token = getToken()
  const isStale = () => Boolean(opts?.signal?.aborted) || getToken() !== token
  const sep = url.includes('?') ? '&' : '?'
  const fullUrl = opts?.lastEventId
    ? `${url}${sep}last_event_id=${encodeURIComponent(opts.lastEventId)}`
    : url

  return (async () => {
    let res: Response
    try {
      res = await fetch(fullUrl, {
        headers: {
          Authorization: token ? `Bearer ${token}` : '',
          Accept: 'text/event-stream',
        },
        signal: opts?.signal,
      })
    } catch {
      return isStale() ? { reason: 'aborted' } : { reason: 'network' }
    }

    if (isStale()) {
      await res.body?.cancel().catch(() => {})
      return { reason: 'aborted' }
    }
    if (res.status === 401) {
      dispatchUnauthorized(token)
      return { reason: 'unauthorized', status: 401 }
    }
    if (res.status === 410) {
      let body: unknown = null
      try {
        body = await res.json()
      } catch {
        /* 非 JSON 响应 */
      }
      if (isStale()) return { reason: 'aborted' }
      return { reason: 'expired', status: 410, body }
    }
    if (!res.ok) {
      return { reason: 'error', status: res.status }
    }
    if (!res.body) return { reason: 'eof' }

    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    let terminalSeen = false
    try {
      for (;;) {
        if (isStale()) {
          await reader.cancel().catch(() => {})
          return { reason: 'aborted' }
        }
        const { done, value } = await reader.read()
        if (done) break
        if (isStale()) {
          await reader.cancel().catch(() => {})
          return { reason: 'aborted' }
        }
        buf += decoder.decode(value, { stream: true })
        const { frames, rest } = takeFrames(buf)
        buf = rest
        for (const frame of frames) {
          if (isStale()) {
            await reader.cancel().catch(() => {})
            return { reason: 'aborted' }
          }
          const parsed = parseSSEFrame(frame)
          if (parsed.data === null) continue // 纯注释帧（心跳）
          const ev = parseData(parsed.data)
          if (!ev) continue // 坏 data 跳帧
          onEvent(ev)
          if (parsed.id) opts?.onLastId?.(parsed.id)
          if (ev.type === 'status' && isTerminalStatus(ev.status)) terminalSeen = true
        }
        if (terminalSeen) {
          await reader.cancel().catch(() => {})
          return { reason: 'terminal' }
        }
        if (opts?.signal?.aborted) {
          await reader.cancel().catch(() => {})
          return { reason: 'aborted' }
        }
      }
    } catch {
      return isStale() ? { reason: 'aborted' } : { reason: 'eof' }
    }
    return isStale() ? { reason: 'aborted' } : { reason: terminalSeen ? 'terminal' : 'eof' }
  })()
}
