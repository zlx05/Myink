// 右栏按章过滤纯函数单测（§11：批次按 :ch{seq} 子线程切 / 单章按 activeChapterSeq 对齐）。
import { describe, expect, it } from 'vitest'
import { chapterRunsOf, chapterToOpenAfterTask, chapterToOpenForPendingTask, latestActiveGenerationTask, latestChapterAwaitingReview, latestGenerationTask, nodesForChapter, runsForChapter } from './taskChapter'
import type { AgentRun, ChapterMeta, TaskSummary } from '../types'

const runs: AgentRun[] = [
  { task_id: 'batch:ch1', node: 'recall', model_id: null, input_tokens: 1, output_tokens: 1, cache_hit: false, duration_ms: 1, cost_est: 0.1, retry_count: 0, degraded: false, error: null, detail: null },
  { task_id: 'batch:ch2', node: 'persist', model_id: null, input_tokens: 1, output_tokens: 1, cache_hit: false, duration_ms: 1, cost_est: 0.2, retry_count: 0, degraded: false, error: null, detail: null },
  { task_id: 'batch:ch3', node: 'audit', model_id: null, input_tokens: 1, output_tokens: 1, cache_hit: false, duration_ms: 1, cost_est: 0.3, retry_count: 0, degraded: false, error: null, detail: null },
]

const nodes = [
  { taskId: 'batch:ch1', node: 'recall', seenAt: 1 },
  { taskId: 'batch:ch2', node: 'persist', seenAt: 2 },
]

describe('runsForChapter', () => {
  it('批次按 :ch{seq} 子线程切该章运行', () => {
    const out = runsForChapter(runs, {
      taskId: 'batch',
      batch: true,
      selectedSeq: 2,
      activeChapterSeq: null,
    })
    expect(out.map((r) => r.node)).toEqual(['persist'])
  })

  it('单章任务按 activeChapterSeq 对齐：匹配章全量，不匹配章为空', () => {
    const single = runs.slice(0, 1).map((r) => ({ ...r, task_id: 'single' }))
    const match = runsForChapter(single, {
      taskId: 'single',
      batch: false,
      selectedSeq: 1,
      activeChapterSeq: 1,
    })
    expect(match).toHaveLength(1)
    const miss = runsForChapter(single, {
      taskId: 'single',
      batch: false,
      selectedSeq: 2,
      activeChapterSeq: 1,
    })
    expect(miss).toHaveLength(0)
  })

  it('未选中章或无活动任务不过滤（整体兜底）', () => {
    expect(runsForChapter(runs, { taskId: 'batch', batch: true, selectedSeq: null })).toEqual(runs)
    expect(runsForChapter(runs, { taskId: null, batch: true, selectedSeq: 1 })).toEqual(runs)
  })
})

describe('nodesForChapter', () => {
  it('批次按 :ch{seq} 切实时节点流', () => {
    const out = nodesForChapter(nodes, {
      taskId: 'batch',
      batch: true,
      selectedSeq: 1,
      activeChapterSeq: null,
    })
    expect(out.map((n) => n.node)).toEqual(['recall'])
  })

  it('单章任务按 activeChapterSeq 对齐', () => {
    const out = nodesForChapter(nodes, {
      taskId: 'batch',
      batch: false,
      selectedSeq: 1,
      activeChapterSeq: 1,
    })
    expect(out).toEqual(nodes)
  })
})

describe('chapterRunsOf', () => {
  it('批次详情按 :ch{seq} 切片，单章任务全量', () => {
    expect(chapterRunsOf('batch_generate', 'batch', runs, 2).map((r) => r.node)).toEqual(['persist'])
    expect(chapterRunsOf('chapter_generate', 'single', runs, 1)).toHaveLength(3)
  })

  it('未选中章 → 全量', () => {
    expect(chapterRunsOf('batch_generate', 'batch', runs, null)).toEqual(runs)
  })
})

describe('latestGenerationTask', () => {
  const summary = (task_id: string, task_type: string): TaskSummary => ({
    task_id, task_type, status: 'done', chapter_seq: 2, batch_size: null,
    batch_current: null, cost_total: 0, error: null, created_at: null,
  })

  it('只选择倒序列表中最新一次章节生成，不让其他任务变成第二份流程图', () => {
    const tasks = [summary('audit', 'global_audit'), summary('latest', 'chapter_generate'), summary('old', 'batch_generate')]
    expect(latestGenerationTask(tasks)?.task_id).toBe('latest')
    expect(latestGenerationTask([summary('audit', 'global_audit')])).toBeNull()
  })

  it('切书恢复只接还在跑的生成任务', () => {
    const running: TaskSummary = {
      ...summary('live', 'chapter_generate'),
      status: 'running',
      chapter_seq: 3,
    }
    expect(latestActiveGenerationTask([summary('done', 'chapter_generate'), running])?.task_id).toBe('live')
    expect(latestActiveGenerationTask([summary('done', 'chapter_generate')])).toBeNull()
  })
})

describe('chapterToOpenAfterTask', () => {
  const chapter = (id: string, chapter_seq: number): ChapterMeta => ({
    id, chapter_seq, title: null, status: 'confirmed', word_count: null, summary: null,
  })

  it('单章转待确认后切到刚生成的章，而不是继续停在上一章', () => {
    const chapters = [chapter('c16', 16), { ...chapter('c17', 17), status: 'awaiting_review' as const }]
    expect(chapterToOpenAfterTask(chapters, 17, 'c16')?.id).toBe('c17')
  })

  it('批次任务保留用户选择；首次无选择时打开最早一章', () => {
    const chapters = [chapter('c2', 2), chapter('c1', 1)]
    expect(chapterToOpenAfterTask(chapters, null, 'c2')).toBeNull()
    expect(chapterToOpenAfterTask(chapters, null, null)?.id).toBe('c1')
  })

  it('页面恢复时选择最新的待确认章节', () => {
    const chapters = [
      chapter('c16', 16),
      { ...chapter('c17', 17), status: 'awaiting_review' as const },
      { ...chapter('c12', 12), status: 'awaiting_review' as const },
    ]
    expect(latestChapterAwaitingReview(chapters)?.id).toBe('c17')
    expect(latestChapterAwaitingReview([chapter('c16', 16)])).toBeNull()
  })

  it('历史终态任务不会把用户从其他章节拉回第 17 章', () => {
    const chapters = [chapter('c16', 16), { ...chapter('c17', 17), status: 'awaiting_review' as const }]
    expect(chapterToOpenForPendingTask(chapters, null, 'task-17', 'terminal', 'c16')).toBeNull()
    expect(chapterToOpenForPendingTask(
      chapters, { taskId: 'task-17', chapterSeq: 17 }, 'task-16', 'terminal', 'c16',
    )).toBeNull()
  })

  it('本页刚发起的任务结束时仍会自动打开生成章节', () => {
    const chapters = [chapter('c16', 16), { ...chapter('c17', 17), status: 'awaiting_review' as const }]
    expect(chapterToOpenForPendingTask(
      chapters, { taskId: 'task-17', chapterSeq: 17 }, 'task-17', 'terminal', 'c16',
    )?.id).toBe('c17')
  })
})
