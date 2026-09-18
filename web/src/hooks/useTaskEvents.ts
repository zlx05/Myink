// 任务进度状态机：SSE 实时节点流（fetch 客户端）→ 终态/410 过期回退 GET /tasks/:id 快照。
// 断线（网络中断 / 网关 30min 硬超时）指数退避重连，带 last_event_id 追平（Redis 流可重放）。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import { openSSE, type SSEEvent } from '../lib/sse'
import type { AgentRun, TaskStatus } from '../types'

export type TaskPhase =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'terminal'
  | 'expired'
  | 'error'

export interface NodeEvent {
  taskId: string
  node: string
  seenAt: number
}

export interface ArtifactState {
  artifactId: string
  taskId: string
  stage: 'plan' | 'write' | string
  chapterSeq: number
  attempt: number
  content: string
  complete: boolean
  failed: boolean
  artifact: unknown | null
  message: string | null
}

export interface TaskEventState {
  phase: TaskPhase
  status: TaskStatus | null
  /** 实时节点流顺序（SSE 只带 node 名，无 token/cost 元数据） */
  nodes: NodeEvent[]
  /** Plan/正文流式产物；同章同阶段只保留当前一次有效尝试。 */
  artifacts: ArtifactState[]
  /** 终态/过期后从 GET /tasks/:id 快照填充（含全部元数据） */
  runs: AgentRun[]
  /** 批次进度 i/N（快照权威值；实时用 nodes 的 persist 去重计数近似） */
  progress: { current: number; total: number } | null
  error: string | null
  payload: Record<string, unknown>
  lastEventId: string | null
  stop: () => void
  retry: () => void
  /** 外部控制（pause/resume/cancel）后主动拉 GET /tasks/:id 快照刷新状态/进度；
   *  若为终态（如取消成功）→ 停流并切 phase=terminal（外部控制不发 SSE 事件，流不会自终） */
  refresh: () => void
}

export function isTerminalPhase(phase: TaskPhase): boolean {
  return phase === 'terminal' || phase === 'expired' || phase === 'error'
}

// 与网关 sse.go 的关流终态保持一致。
const TERMINAL_STATUSES = new Set<TaskStatus>([
  'done', 'failed', 'cancelled', 'awaiting_plan', 'awaiting_review',
])

// 落库节点事件触发的快照合并窗口：20 章批次每章约 10 个节点，逐事件拉整任务详情是
// O(节点数) 次全量请求（批量下近似平方级）。窗口内最多拉一次，末态仍必被拉到。
const SNAPSHOT_DEBOUNCE_MS = 250

export function useTaskEvents(
  taskId: string | null,
  opts?: { batchTotal?: number; resumeKey?: number | null },
): TaskEventState {
  const [phase, setPhase] = useState<TaskPhase>('idle')
  const [status, setStatus] = useState<TaskStatus | null>(null)
  const [nodes, setNodes] = useState<NodeEvent[]>([])
  const [artifacts, setArtifacts] = useState<ArtifactState[]>([])
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [progress, setProgress] = useState<{ current: number; total: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [payload, setPayload] = useState<Record<string, unknown>>({})
  const [lastEventId, setLastEventId] = useState<string | null>(null)

  // 累积态放 ref，重连闭包不读旧值
  const abortRef = useRef<AbortController | null>(null)
  const connectedTaskRef = useRef<string | null>(null)
  const nodesRef = useRef<NodeEvent[]>([])
  const artifactRef = useRef(new Map<string, ArtifactState>())
  const lastIdRef = useRef<string | null>(null)
  // 状态推进序号：SSE 的 status 事件与每次应用的快照都 +1。快照请求发起时记下当时序号，
  // 回来时序号已变（期间来了更新的状态）就只更新 runs、不覆盖 status。
  const statusSeqRef = useRef(0)
  const snapTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fetchSnapshot = useCallback(async (tid: string, forceTerminal = false): Promise<TaskStatus | null> => {
    const statusSeq = statusSeqRef.current
    try {
      const detail = await api.getTask(tid)
      // 切章期间旧请求可能晚于新请求返回，不能让上一章快照覆盖当前章节。
      if (connectedTaskRef.current !== tid) return null
      setRuns(detail.runs)
      setPayload(detail.payload)
      setError(detail.error)
      if (detail.progress) setProgress(detail.progress)
      // 状态写入要论新旧：SSE 的 status 事件比快照新（快照是请求发起时的视图），
      // 晚回的旧快照覆盖它，右栏就会出现「左边已 write、右边还在 plan」。
      // forceTerminal = 外部控制后的权威核对，无条件写入。
      if (forceTerminal || statusSeq === statusSeqRef.current) {
        setStatus(detail.status)
        statusSeqRef.current += 1
      }
      if (forceTerminal && TERMINAL_STATUSES.has(detail.status)) {
        // 外部取消：流不会自己终态 → 主动停流；connect 循环在 openSSE 返回后先查
        // controller.signal.aborted 早退，不会把 phase 误写成 error
        abortRef.current?.abort()
        setPhase('terminal')
      }
      return detail.status
    } catch {
      // 任务在途（入队→DB 物化窗口内 404）时快照失败：保留现场，交手动 retry
      return null
    }
  }, [])

  // 节点事件快照合并（见 SNAPSHOT_DEBOUNCE_MS）：窗口内最多拉一次；窗口结束仍在工作
  // （取数期间又来事件）就再开一轮，保证最后一个节点对应的 runs 一定被拉到。
  const scheduleSnapshot = useCallback((tid: string) => {
    if (snapTimerRef.current !== null) return
    snapTimerRef.current = setTimeout(() => {
      snapTimerRef.current = null
      void fetchSnapshot(tid)
    }, SNAPSHOT_DEBOUNCE_MS)
  }, [fetchSnapshot])

  const refresh = useCallback(() => {
    if (taskId) void fetchSnapshot(taskId, true)
  }, [taskId, fetchSnapshot])

  const stop = useCallback(() => {
    abortRef.current?.abort()
    if (snapTimerRef.current !== null) {
      clearTimeout(snapTimerRef.current)
      snapTimerRef.current = null
    }
    nodesRef.current = []
    artifactRef.current.clear()
    lastIdRef.current = null
    connectedTaskRef.current = null
    setNodes([])
    setArtifacts([])
    setStatus(null)
    setRuns([])
    setProgress(null)
    setError(null)
    setPayload({})
    setLastEventId(null)
    setPhase('idle')
  }, [])

  const connect = useCallback(
    async (tid: string) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      const onEvent = (ev: SSEEvent) => {
        if (ev.status) {
          statusSeqRef.current += 1
          setStatus(ev.status as TaskStatus)
        }
        if (ev.node) {
          nodesRef.current = [
            ...nodesRef.current,
            { taskId: ev.task_id, node: ev.node, seenAt: Date.now() },
          ]
          setNodes(nodesRef.current)
          // 每个节点落库即拉一次快照：右栏的耗时/费用视图（runs）只认快照，若只在
          // route/persist 刷新，写作完成后要等到审核结束才更新，期间一直停在上一节点。
          // 走合并窗口（SNAPSHOT_DEBOUNCE_MS）：节点密集时不会打出一串全量详情请求。
          scheduleSnapshot(tid)
        }
        if (ev.type.startsWith('artifact_') && ev.stage && ev.chapter_seq) {
          const key = `${ev.stage}:${ev.chapter_seq}`
          if (ev.type === 'artifact_reset') {
            artifactRef.current.set(key, {
              artifactId: ev.artifact_id,
              taskId: ev.task_id,
              stage: ev.stage,
              chapterSeq: ev.chapter_seq,
              attempt: ev.attempt,
              content: '',
              complete: false,
              failed: false,
              artifact: null,
              message: null,
            })
          } else {
            const previous = artifactRef.current.get(key)
            const current: ArtifactState = previous?.artifactId === ev.artifact_id ? previous : {
              artifactId: ev.artifact_id,
              taskId: ev.task_id,
              stage: ev.stage,
              chapterSeq: ev.chapter_seq,
              attempt: ev.attempt,
              content: '', complete: false, failed: false, artifact: null, message: null,
            }
            let content = current.content
            if (ev.type === 'artifact_delta') {
              // Python offset 按 Unicode code point 计数；JS length/slice 按 UTF-16 code unit。
              // 用 Array.from 对齐，避免正文含 emoji/罕见扩展字符时从代理对中间截断。
              const points = Array.from(content)
              if (ev.offset === points.length) content += ev.content
              else if (ev.offset < points.length) content = points.slice(0, ev.offset).join('') + ev.content
              else content += ev.content
            }
            let artifact = current.artifact
            if (ev.artifact) {
              try { artifact = JSON.parse(ev.artifact) as unknown } catch { artifact = null }
            }
            artifactRef.current.set(key, {
              ...current,
              content,
              complete: ev.type === 'artifact_complete' ? true : current.complete,
              failed: ev.type === 'artifact_failed' ? true : current.failed,
              artifact,
              message: ev.message || current.message,
            })
          }
          setArtifacts(Array.from(artifactRef.current.values()))
        }
      }

      let attempt = 0
      setPhase('connecting')
      for (;;) {
        if (controller.signal.aborted) return
        const result = await openSSE(`/api/v1/tasks/${tid}/events`, onEvent, {
          lastEventId: lastIdRef.current ?? undefined,
          signal: controller.signal,
          onLastId: (id) => {
            lastIdRef.current = id
            setLastEventId(id)
          },
        })
        if (controller.signal.aborted) return

        switch (result.reason) {
          case 'terminal':
            // 同一个 task_id 会在 awaiting_plan / awaiting_review 后续跑。旧网关或竞争
            // 窗口可能先回放历史终态，而 DB 权威状态此时已经是 queued/running；不能就此
            // 停止监听，否则任务真正 done 后章节列表仍停在“待确认”，下一章也会被锁住。
            // 先核对快照：仍在运行就退避后重连（同时形成低频状态轮询兜底），只有快照
            // 也处于终态或暂时不可用时才接受本次 SSE 终态。
            {
              const snapshotStatus = await fetchSnapshot(tid)
              if (controller.signal.aborted) return
              if (snapshotStatus && !TERMINAL_STATUSES.has(snapshotStatus)) {
                setPhase('reconnecting')
                attempt += 1
                const delay = Math.min(4000, 500 * 2 ** Math.min(attempt - 1, 3))
                await new Promise((resolve) => setTimeout(resolve, delay))
                continue
              }
              setPhase('terminal')
              return
            }
          case 'expired': {
            // 流被裁剪（Redis key 过期）→ 回退 GET 快照。但任务可能仍在跑：worker 若
            // 长时间无事件，key 会先过期而任务尚未落终态。此时不能直接收尾，因为
            // 'expired' 不在在途判定里，写按钮会在任务进行中被放出来造成重复提交。
            const snapshotStatus = await fetchSnapshot(tid)
            if (controller.signal.aborted) return
            if (snapshotStatus && !TERMINAL_STATUSES.has(snapshotStatus)) {
              setPhase('reconnecting')
              attempt += 1
              const delay = Math.min(8000, 1000 * 2 ** Math.min(attempt - 1, 3))
              await new Promise((resolve) => setTimeout(resolve, delay))
              continue
            }
            setPhase('expired')
            return
          }
          case 'unauthorized':
            // token.ts 已派发 401 登出事件，路由守卫自动跳登录
            setPhase('error')
            return
          case 'network':
          case 'eof': {
            // 流结束但未达终态。存在两种成因：任务仍在跑（正常断线），或 Redis 流还在
            // 而 worker 已死、终态帧永不到达（网关 30min 硬超时后才 EOF）。先核对快照
            // 区分二者：已是终态就地收尾，否则退避重连（重连兼作低频状态轮询兜底）。
            const snapshotStatus = await fetchSnapshot(tid)
            if (controller.signal.aborted) return
            if (snapshotStatus && TERMINAL_STATUSES.has(snapshotStatus)) {
              setPhase('terminal')
              return
            }
            setPhase('reconnecting')
            attempt += 1
            const delay = Math.min(8000, 1000 * 2 ** Math.min(attempt - 1, 3))
            await new Promise((resolve) => setTimeout(resolve, delay))
            continue
          }
          case 'error':
          case 'aborted':
            setPhase('error')
            return
        }
      }
    },
    [fetchSnapshot, scheduleSnapshot],
  )

  const retry = useCallback(() => {
    if (taskId) void connect(taskId)
  }, [taskId, connect])

  // 实时批次 i/N：SSE 只带节点名，用 persist 节点章级 task_id 去重计数（与 Python
  // progress.current 口径一致，§阶段2）；快照到达后以快照为权威。
  const liveCurrent = useMemo(
    () => new Set(nodes.filter((n) => n.node === 'summarize').map((n) => n.taskId)).size,
    [nodes],
  )
  const effectiveProgress =
    progress ?? (opts?.batchTotal ? { current: liveCurrent, total: opts.batchTotal } : null)

  useEffect(() => {
    if (!taskId) {
      stop()
      return
    }
    // 切换章节加载另一条任务时，先清掉上一章快照，避免短暂展示错误的流程和审核报告。
    if (connectedTaskRef.current !== taskId) {
      nodesRef.current = []
      artifactRef.current.clear()
      lastIdRef.current = null
      setNodes([])
      setArtifacts([])
      setStatus(null)
      setRuns([])
      setProgress(null)
      setError(null)
      setPayload({})
      setLastEventId(null)
      statusSeqRef.current = 0
      connectedTaskRef.current = taskId
    }
    // 持久化快照先行：旧任务的 Redis 流可能仍存在却不会再产生终态事件，若只等 SSE
    // 结束，重新进入页面会一直显示空流程。实时任务随后继续由 SSE 增量更新。
    void fetchSnapshot(taskId)
    void connect(taskId)
    return () => {
      abortRef.current?.abort()
      if (snapTimerRef.current !== null) {
        clearTimeout(snapTimerRef.current)
        snapTimerRef.current = null
      }
    }
    // taskId 变化或 resumeKey 变化才重连；stop/connect 闭包捕获最新 ref。
    // resumeKey：awaiting_review 终态关流后「放行本章」复用同一 task_id 续跑，
    // taskId 不变不会触发重连，靠 resumeKey 自增强制重开流（last_event_id 追平）。
  }, [taskId, connect, fetchSnapshot, stop, opts?.resumeKey])

  return {
    phase,
    status,
    nodes,
    artifacts,
    runs,
    progress: effectiveProgress,
    error,
    payload,
    lastEventId,
    stop,
    retry,
    refresh,
  }
}
