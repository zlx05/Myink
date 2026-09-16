// 生成入口：写下一章（永远写「已写最大章 + 1」的下一未写章）+ 重写本章（仅 confirmed 章，
// 带确认弹窗）+ 批次生成（N≤20，成本估算标注「估算」）。
// 失败 → 中文横幅；成功 → onTaskStart(taskId) 交给时间线。
import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import type { ChapterMeta, WritingMode } from '../types'
import styles from './GenerationPanel.module.css'

/** 单章估算成本（与 gateway config.go 默认值对齐；无 API 暴露，前端常量标注「估算」） */
const COST_PER_CHAPTER = 0.05
const BATCH_MAX = 20

interface Props {
  projectId: string
  chapters: ChapterMeta[]
  selectedChapter: ChapterMeta | null
  /** 当前书已有任务在规划/写作/等待人工时，阻止重复发起。 */
  taskBusy?: boolean
  /** 批次生成时带 batchTotal（时间线实时 i/N）；单章任务带 chapterSeq（右栏按章过滤） */
  onTaskStart: (taskId: string, batchTotal?: number, chapterSeq?: number, mode?: WritingMode) => void
}

export function GenerationPanel({ projectId, chapters, selectedChapter, taskBusy = false, onTaskStart }: Props) {
  const [open, setOpen] = useState(true)
  const [instruction, setInstruction] = useState('')
  const [batchN, setBatchN] = useState(3)
  const [mode, setMode] = useState<WritingMode>('auto')
  const [busy, setBusy] = useState<null | 'chapter' | 'batch'>(null)
  const [banner, setBanner] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    setInstruction('')
    setBusy(null)
    setBanner(null)
    setNotice(null)
  }, [projectId])

  // 已写最大章序 + 下一章序号（空项目 → 1，与 worker _guard_write_order 语义一致）。
  // 章节列表只有已物化行：写下一章 = seq 恒为 max_seq+1，绝不踩「选已写章被守卫拒绝」。
  const tail = chapters.reduce<ChapterMeta | null>(
    (current, chapter) => (
      current === null || chapter.chapter_seq > current.chapter_seq ? chapter : current
    ),
    null,
  )
  const tailIsEmptyPlaceholder = Boolean(
    tail
      && (tail.word_count ?? 0) === 0
      && ['planning', 'writing', 'failed', 'cancelled'].includes(tail.status),
  )
  const nextSeq = tailIsEmptyPlaceholder && tail ? tail.chapter_seq : (tail?.chapter_seq ?? 0) + 1
  const chapterIsPlanning = tailIsEmptyPlaceholder && tail?.status === 'planning'
  const chapterIsRunning = tailIsEmptyPlaceholder && tail?.status === 'writing'
  const chapterIsBusy = chapterIsPlanning || chapterIsRunning
  const controlsBusy = busy !== null || chapterIsBusy || taskBusy
  const chapterActionLabel = chapterIsRunning
    ? `第 ${nextSeq} 章生成中…`
    : chapterIsPlanning
      ? `第 ${nextSeq} 章等待计划确认`
    : tailIsEmptyPlaceholder
      ? `继续生成第 ${nextSeq} 章`
      : `写下一章（第 ${nextSeq} 章）`
  // cid=尾部章占位：worker 全程忽略 chapter_id（grep 零匹配），写序由 seq 权威。
  // 空书无已物化章 → 用 projectId 占位（后端同样放行 seq=1，见 worker _guard_write_order）。
  const tailId = tail?.id ?? projectId
  const batchStart = nextSeq
  const batchCost = (batchN * COST_PER_CHAPTER).toFixed(2)

  function showError(err: unknown) {
    setNotice(null)
    if (err instanceof ApiError) {
      setBanner(formatApiError(err))
    } else {
      setBanner('请求失败，请重试')
    }
  }

  // 写下一章：空书也可写（第 1 章，写作指令透传）；tailId 恒非空（空书回退 projectId 占位）
  async function generateNextChapter() {
    if (busy) return
    setBusy('chapter')
    setBanner(null)
    setNotice(null)
    try {
      const resp = await api.generateChapter(projectId, tailId, {
        seq: nextSeq,
        mode,
        ...(instruction.trim() ? { user_instruction: instruction.trim() } : {}),
      })
      onTaskStart(resp.task_id, undefined, nextSeq, mode)
      setNotice(`第 ${nextSeq} 章写作任务已创建，进度会显示在下方“章节流转”中。`)
    } catch (err) {
      showError(err)
    } finally {
      setBusy(null)
    }
  }

  // 重写本章（仅 confirmed 章；awaiting_review 走时间线 resume/reject 分流）：
  // worker _guard_write_order(rewrite=True) 放行已确认章，重写会失效重建该章相关记忆
  async function rewriteChapter() {
    if (!selectedChapter || selectedChapter.status !== 'confirmed' || busy) return
    if (!window.confirm(`重写第 ${selectedChapter.chapter_seq} 章？将失效重建该章相关记忆，不可撤销。`)) return
    setBusy('chapter')
    setBanner(null)
    setNotice(null)
    try {
      const resp = await api.generateChapter(projectId, selectedChapter.id, {
        seq: selectedChapter.chapter_seq,
        rewrite: true,
        mode,
        ...(instruction.trim() ? { user_instruction: instruction.trim() } : {}),
      })
      onTaskStart(resp.task_id, undefined, selectedChapter.chapter_seq, mode)
      setNotice(`第 ${selectedChapter.chapter_seq} 章重写任务已创建，进度会显示在下方“章节流转”中。`)
    } catch (err) {
      showError(err)
    } finally {
      setBusy(null)
    }
  }

  async function generateBatch() {
    if (busy || mode === 'manual') return
    setBusy('batch')
    setBanner(null)
    setNotice(null)
    try {
      const resp = await api.generateBatch(projectId, {
        size: batchN,
        start: batchStart,
      })
      onTaskStart(resp.task_id, batchN, batchStart)
      setNotice(`批次任务已创建，将从第 ${batchStart} 章开始写作。`)
    } catch (err) {
      showError(err)
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className={`panel ${styles.panel}`}>
      <header className={styles.head}>
        <button type="button" className={styles.toggle} onClick={() => setOpen((value) => !value)} aria-expanded={open}>
          <span className={styles.heading}>生成与重写</span>
          <span className={styles.next}>
            {chapterIsRunning
              ? `当前生成：第 ${nextSeq} 章`
              : chapterIsPlanning
                ? `等待确认：第 ${nextSeq} 章`
              : tailIsEmptyPlaceholder
                ? `待继续：第 ${nextSeq} 章`
                : `下一章：第 ${nextSeq} 章`}
          </span>
          <span className={styles.caret}>{open ? '▾' : '▸'}</span>
        </button>
      </header>

      {open && <div className={styles.body}>

      {banner && <div className="banner banner-error" role="alert">{banner}</div>}
      {notice && <div className={styles.notice} role="status">{notice}</div>}

      <form className={styles.form} onSubmit={(event) => { event.preventDefault(); void generateNextChapter() }}>
        <label className={styles.label} htmlFor="user-instruction">
          写作指令（可留空）
        </label>
        <textarea
          id="user-instruction"
          className="textarea"
          rows={2}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="如：节奏放慢，重点刻画战斗场面"
        />
        <button
          type="button"
          className="btn btn-primary"
          disabled={controlsBusy}
          onClick={() => void generateNextChapter()}
        >
          {busy === 'chapter' ? '发起中…' : chapterActionLabel}
        </button>
        <fieldset className={styles.modeSwitch}>
          <legend>写作模式</legend>
          <button
            type="button"
            className={mode === 'auto' ? styles.modeActive : styles.modeButton}
            aria-pressed={mode === 'auto'}
            disabled={controlsBusy}
            onClick={() => setMode('auto')}
          >
            自动
          </button>
          <button
            type="button"
            className={mode === 'manual' ? styles.modeActive : styles.modeButton}
            aria-pressed={mode === 'manual'}
            disabled={controlsBusy}
            onClick={() => setMode('manual')}
          >
            手动确认 Plan
          </button>
          <span>{mode === 'manual' ? 'Plan 可编辑，确认后才开始写作' : '展示 Plan 后自动写作'}</span>
        </fieldset>
        <button
          type="button"
          className="btn btn-quiet"
          disabled={!selectedChapter || selectedChapter.status !== 'confirmed' || controlsBusy}
          onClick={rewriteChapter}
        >
          {selectedChapter && selectedChapter.status === 'confirmed'
            ? `重写第 ${selectedChapter.chapter_seq} 章`
            : '重写本章'}
        </button>
        {chapters.length === 0 && (
          <p className={styles.emptyHint}>
            新书可直接写第 1 章（上面的写作指令会生效）；要一次写多章再用下方「发起批次」。
          </p>
        )}
      </form>

      <form className={styles.form} onSubmit={(event) => { event.preventDefault(); void generateBatch() }}>
        <label className={styles.label} htmlFor="batch-n">
          批次生成
        </label>
        <div className={styles.batchRow}>
          <input
            id="batch-n"
            type="number"
            min={1}
            max={BATCH_MAX}
            value={batchN}
            disabled={mode === 'manual'}
            onChange={(e) => setBatchN(Math.max(1, Math.min(BATCH_MAX, Number(e.target.value))))}
          />
          <span className={styles.batchHint}>
            从第 {batchStart} 章起
            <span className={styles.cost}> ≈ ¥{batchCost}（估算）</span>
          </span>
        </div>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={controlsBusy || mode === 'manual'}
          onClick={() => void generateBatch()}
        >
          {busy === 'batch' ? '发起中…' : '发起批次'}
        </button>
        {mode === 'manual' && <p className={styles.batchDisabled}>手动模式需要逐章确认计划，批量生成已禁用。</p>}
      </form>
      </div>}
    </section>
  )
}
