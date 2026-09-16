// 切书只卸本页 React 状态，不卸 Redis 闸门里的进行中任务。
// worker 接手前 PostgreSQL 还没有 Task/章节行，所以把本页发起的写作记在 sessionStorage，
// 切回来才能继续接 SSE，而不是看起来像没在写。

export interface ActiveWrite {
  taskId: string
  chapterSeq: number
  batchTotal: number | null
}

const keyFor = (projectId: string) => `aiink.activeWrite.${projectId}`

export function readActiveWrite(projectId: string): ActiveWrite | null {
  if (!projectId) return null
  try {
    const raw = sessionStorage.getItem(keyFor(projectId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<ActiveWrite>
    const chapterSeq = Number(parsed.chapterSeq)
    if (!parsed.taskId || !Number.isInteger(chapterSeq) || chapterSeq < 1) return null
    return {
      taskId: String(parsed.taskId),
      chapterSeq,
      batchTotal: typeof parsed.batchTotal === 'number' ? parsed.batchTotal : null,
    }
  } catch {
    return null
  }
}

export function writeActiveWrite(projectId: string, value: ActiveWrite): void {
  if (!projectId) return
  sessionStorage.setItem(keyFor(projectId), JSON.stringify(value))
}

export function clearActiveWrite(projectId: string): void {
  if (!projectId) return
  sessionStorage.removeItem(keyFor(projectId))
}
