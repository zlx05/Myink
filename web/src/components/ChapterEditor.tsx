// 章节编辑器：读正文（GET 单章详情）→ 纸张色 textarea → Ctrl+S / 按钮保存（PUT content 轻编辑）。
// 阶段 4：头部「历史版本」入口 → 版本列表/回退弹层（覆盖写前快照，版本表）。
import { useState, type KeyboardEvent } from 'react'
import { api } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import { getSession } from '../lib/token'
import { useChapterDraft } from '../hooks/useChapterDraft'
import { chapterStatusLabel, chapterStatusTone } from '../lib/labels'
import type { ChapterMeta } from '../types'
import { StatusBadge } from './StatusBadge'
import { VersionHistory } from './VersionHistory'
import styles from './ChapterEditor.module.css'

interface Props {
  projectId: string
  chapter: ChapterMeta
  /** 章节不存在（404，可能被级联删除）→ 由父级刷新列表并清空选择 */
  onNotFound: () => void
  /** 保存成功 → 父级可刷新章节列表（status/version 变化） */
  onSaved: () => void
  /** 记忆校正生成变更集进待确认池 → 父级重载候选池 */
  onMemoryChanged: () => void
  /** 生成任务终态后自增 → 重新拉取正文（同章再生内容已更新） */
  refreshTick?: number
}

export function ChapterEditor(props: Props) {
  const draftKey = `myink.draft:${getSession()?.userId ?? 'anonymous'}:${props.projectId}:${props.chapter.id}`
  return <ChapterEditorBody key={draftKey} {...props} draftKey={draftKey} />
}

function ChapterEditorBody({
  projectId,
  chapter,
  onNotFound,
  onSaved,
  onMemoryChanged,
  refreshTick = 0,
  draftKey,
}: Props & { draftKey: string }) {
  const [restoreTick, setRestoreTick] = useState(0)
  const editor = useChapterDraft(draftKey, projectId, chapter.id, refreshTick + restoreTick, onNotFound, onSaved)
  const { detail, draft, dirty, saving } = editor
  const [actionError, setError] = useState<string | null>(null)
  const [memBusy, setMemBusy] = useState(false)
  const [delBusy, setDelBusy] = useState(false)
  const [memBanner, setMemBanner] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)

  // 内联 handler：每次渲染取最新 save/content，避免 useCallback 闭包存陈旧正文
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault()
      if (dirty && !editor.conflict && !showHistory) void editor.save()
    }
  }

  // 回退成功：就地覆盖编辑器正文/版本（restore 已持久化，无需重拉），版本列表随之变化
  function handleRestored() {
    setShowHistory(false)
    onSaved()
    setRestoreTick((tick) => tick + 1)
  }

  // 校正记忆（§7.3）：编辑后正文重新抽取 → 与该章已落库记忆 diff → 变更集进待确认池。
  // 同步 LLM 调用（一次 extract），超时/失败可重试；no_op = 纯风格编辑无记忆差异。
  async function correctMemory() {
    if (memBusy || dirty || saving) return
    setMemBusy(true)
    setError(null)
    setMemBanner(null)
    try {
      const resp = await api.correctMemory(projectId, chapter.id)
      setMemBanner(
        resp.no_op
          ? '无记忆变更（正文与已落库记忆一致）。'
          : '已生成变更集，进入「待确认候选」池。',
      )
      onMemoryChanged()
    } catch (err) {
      setError(formatApiError(err, '校正失败'))
    } finally {
      setMemBusy(false)
    }
  }

  // 级联删除本章及之后全部章节（正文 + 记忆 + 池候选，进度回退），不可撤销
  async function deleteChapter() {
    if (delBusy) return
    if (
      !window.confirm(
        `删除第 ${chapter.chapter_seq} 章及其后全部章节？正文、记忆与待确认候选将一并移除，不可撤销。`,
      )
    ) {
      return
    }
    setDelBusy(true)
    setError(null)
    try {
      await api.deleteChapter(projectId, chapter.id)
      onNotFound()
    } catch (err) {
      setError(formatApiError(err, '删除失败'))
    } finally {
      setDelBusy(false)
    }
  }

  return (
    <div className={styles.editor}>
      <header className={styles.head}>
        <div className={styles.headRow}>
          <h2 className={styles.title}>
            第 {chapter.chapter_seq} 章{detail?.title ? ` · ${detail.title}` : ''}
          </h2>
          {detail && (
            <div className={styles.actions}>
              <button
                type="button"
                className="btn btn-quiet"
                disabled={memBusy || delBusy || dirty || saving}
                title={dirty ? '请先保存正文，再校正记忆' : undefined}
                onClick={() => void correctMemory()}
              >
                {memBusy ? '校正中…' : '校正记忆'}
              </button>
              <button
                type="button"
                className={`btn ${styles.danger}`}
                disabled={memBusy || delBusy || saving}
                onClick={() => void deleteChapter()}
              >
                {delBusy ? '删除中…' : '删除本章'}
              </button>
              <button
                type="button"
                className="btn btn-quiet"
                disabled={dirty || saving}
                title={dirty ? '请先保存或放弃草稿，再回退版本' : undefined}
                onClick={() => setShowHistory(true)}
              >
                历史版本
              </button>
            </div>
          )}
        </div>
        <div className={styles.metaRow}>
          <StatusBadge tone={chapterStatusTone(chapter.status)}>
            {chapterStatusLabel(chapter.status)}
          </StatusBadge>
          {draft && <span className={styles.version}>v{draft.baseVersion}</span>}
          {detail?.summary && <span className={styles.summary}>{detail.summary}</span>}
        </div>
      </header>

      {(actionError || editor.error) && <div className="banner banner-error">{actionError || editor.error}</div>}
      {editor.backupError && <div className="banner banner-error">浏览器草稿备份失败，请立即保存或复制正文，勿关闭页面。</div>}
      {editor.blocker.state === 'blocked' && <div className="banner banner-error">
        草稿尚未备份，离开可能丢失修改。
        <button className="btn btn-quiet" onClick={() => editor.blocker.reset?.()}>继续编辑</button>
        <button className="btn btn-quiet" onClick={() => editor.blocker.proceed?.()}>仍然离开</button>
      </div>}
      {editor.conflict && detail && <div className="banner">
        服务器已有 v{detail.version}，当前草稿基于 v{draft?.baseVersion}。请比较后处理。
        <details><summary>查看服务器正文</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{detail.content}</pre></details>
        <button className="btn btn-quiet" disabled={saving} onClick={() => {
          if (window.confirm('确认将当前草稿保存为新版本？服务器当前正文会保留在历史版本中。')) void editor.save(detail.version)
        }}>以当前草稿保存为新版本</button>
        <button className="btn btn-quiet" disabled={saving} onClick={editor.acceptRemote}>放弃草稿，载入服务器版本</button>
      </div>}
      {memBanner && <div className="banner">{memBanner}</div>}

      {!detail || !draft ? (
        <div className="empty">{editor.error ? '正文暂不可用' : '加载中…'}</div>
      ) : (
        <>
          <textarea
            className={styles.textarea}
            aria-label="章节正文"
            value={draft.content}
            onChange={(e) => editor.edit(e.target.value)}
            readOnly={showHistory || memBusy || delBusy}
            onKeyDown={onKeyDown}
            spellCheck={false}
            placeholder="章节正文…（Ctrl+S 保存）"
          />
          <footer className={styles.foot}>
            <span className={dirty ? styles.dirty : styles.saved}>
              {dirty ? (editor.backupError ? '未保存修改' : '草稿已备份到本标签页，尚未提交') : '已保存'}
            </span>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void editor.save()}
              disabled={!dirty || saving || editor.conflict || showHistory}
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </footer>
        </>
      )}

      {showHistory && (
        <VersionHistory
          projectId={projectId}
          chapterId={chapter.id}
          chapterSeq={chapter.chapter_seq}
          onClose={() => setShowHistory(false)}
          onRestored={handleRestored}
        />
      )}
    </div>
  )
}
