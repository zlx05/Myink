// 右栏按章过滤的纯函数（§11）：选中某章时，节点流转/花费只显示该章情况。
// 批次按 {batch_id}:ch{seq} 子线程切；单章任务按发起时带出的 activeChapterSeq 对齐。
// 选中章为 null（未选）→ 不过滤（时间线整体兜底，保持原行为）。

import type { AgentRun, ChapterMeta, TaskSummary } from '../types'

/**
 * 单章任务结束后打开刚生成的章节，让 awaiting_review 的候选和流转立即可见。
 * 批次没有 activeChapterSeq：仅在用户尚未选章时打开最早一章。
 */
export function chapterToOpenAfterTask(
  chapters: ChapterMeta[],
  activeChapterSeq: number | null,
  selectedCid: string | null,
): ChapterMeta | null {
  if (activeChapterSeq !== null) {
    return chapters.find((chapter) => chapter.chapter_seq === activeChapterSeq) ?? null
  }
  if (selectedCid || chapters.length === 0) return null
  return [...chapters].sort((a, b) => a.chapter_seq - b.chapter_seq)[0]
}

export interface PendingAutoOpen {
  taskId: string
  chapterSeq: number | null
}

/** 历史终态任务不得触发自动跳章；只有当前页面明确登记的生成任务可以跳一次。 */
export function chapterToOpenForPendingTask(
  chapters: ChapterMeta[],
  pending: PendingAutoOpen | null,
  activeTaskId: string | null,
  taskPhase: string,
  selectedCid: string | null,
): ChapterMeta | null {
  if (taskPhase !== 'terminal' || !pending || pending.taskId !== activeTaskId) return null
  return chapterToOpenAfterTask(chapters, pending.chapterSeq, selectedCid)
}

/** 页面刷新后优先恢复仍在生成或待处理的章节，计划确认页也不能丢。 */
export function latestChapterAwaitingReview(chapters: ChapterMeta[]): ChapterMeta | null {
  return chapters
    .filter((chapter) => ['planning', 'writing', 'awaiting_review'].includes(chapter.status))
    .sort((a, b) => b.chapter_seq - a.chapter_seq)[0] ?? null
}

/** API 已按时间倒序；右栏只取最新一次生成任务，忽略校验/全局审计等其他任务。 */
export function latestGenerationTask(tasks: TaskSummary[]): TaskSummary | null {
  return tasks.find((task) => task.task_type === 'chapter_generate' || task.task_type === 'batch_generate') ?? null
}

const ACTIVE_GENERATION_STATUSES = new Set([
  'queued', 'running', 'paused', 'awaiting_plan', 'awaiting_review',
])

/** 切书回来时只接还在跑/待确认的生成任务，忽略已经结束的历史任务。 */
export function latestActiveGenerationTask(tasks: TaskSummary[]): TaskSummary | null {
  return tasks.find((task) => (
    (task.task_type === 'chapter_generate' || task.task_type === 'batch_generate')
    && ACTIVE_GENERATION_STATUSES.has(task.status)
  )) ?? null
}

export interface ChapterFilterOpts {
  /** 活动任务 id（SSE 时间线）；null = 无活动任务，不过滤 */
  taskId: string | null
  /** 活动任务是否批次（batchTotal 标记）；批次按 :ch{seq} 切，单章按 activeChapterSeq 对齐 */
  batch: boolean
  /** 当前选中章序（编辑器）；null = 未选中，不过滤 */
  selectedSeq: number | null
  /** 活动单章任务对应的章（GenerationPanel 发起时带出）；null = 批次/未知 */
  activeChapterSeq?: number | null
}

/** 选中章 → 该章节点运行记录切片。未选中 / 无活动任务 → 原数组；单章任务标记不匹配 → 空。 */
export function runsForChapter(runs: AgentRun[], opts: ChapterFilterOpts): AgentRun[] {
  const { taskId, batch, selectedSeq, activeChapterSeq = null } = opts
  if (selectedSeq === null || !taskId) return runs
  if (batch) return runs.filter((r) => r.task_id === `${taskId}:ch${selectedSeq}`)
  return activeChapterSeq !== null && activeChapterSeq === selectedSeq ? runs : []
}

/** 选中章 → 该章实时节点流切片（批次按 :ch{seq}，单章按 activeChapterSeq 对齐）。 */
export function nodesForChapter<T extends { taskId: string }>(
  nodes: T[],
  opts: ChapterFilterOpts,
): T[] {
  const { taskId, batch, selectedSeq, activeChapterSeq = null } = opts
  if (selectedSeq === null || !taskId) return nodes
  if (batch) return nodes.filter((n) => n.taskId === `${taskId}:ch${selectedSeq}`)
  return activeChapterSeq !== null && activeChapterSeq === selectedSeq ? nodes : []
}

/** 展开详情按选中章切片：列表已按章过滤，批次详情再按 :ch{seq} 切运行，单章任务全量。 */
export function chapterRunsOf(
  taskType: string,
  taskId: string,
  detailRuns: AgentRun[],
  selectedSeq: number | null,
): AgentRun[] {
  if (selectedSeq === null) return detailRuns
  if (taskType === 'batch_generate') {
    return detailRuns.filter((r) => r.task_id === `${taskId}:ch${selectedSeq}`)
  }
  return detailRuns
}
