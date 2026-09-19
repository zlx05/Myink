// 工作台（三栏）：rail 项目切换 + 章节列表 | 章节编辑器 | 生成入口/时间线/校验报告/候选池。
// 生成任务进度状态在页面级提升：useTaskEvents(activeTaskId)，终态 → 刷新章节列表。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { AuditPanel } from '../components/AuditPanel'
import { CandidatePanel, type ReleaseTarget } from '../components/CandidatePanel'
import { ChapterEditor } from '../components/ChapterEditor'
import { ChapterPlanPanel } from '../components/ChapterPlanPanel'
import { ChapterList } from '../components/ChapterList'
import { GenerationPanel } from '../components/GenerationPanel'
import { LessonsPanel } from '../components/LessonsPanel'
import { ProjectRail } from '../components/ProjectRail'
import { TaskTimeline } from '../components/TaskTimeline'
import { StreamingChapterView } from '../components/StreamingChapterView'
import { useAuth } from '../context/AuthContext'
import { useTaskEvents } from '../hooks/useTaskEvents'
import { api, ApiError } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import { isProjectDraft, projectHref } from '../lib/projectCreation'
import { clearActiveWrite, readActiveWrite, writeActiveWrite } from '../lib/activeWrite'
import { liveStageNode } from '../lib/taskFlow'
import { chapterToOpenForPendingTask, latestActiveGenerationTask, latestChapterAwaitingReview, latestGenerationTask, nodesForChapter, runsForChapter } from '../lib/taskChapter'
import type { ChapterMeta, MemoryCandidate, Project, WritingMode } from '../types'
import styles from './WorkspacePage.module.css'

export default function WorkspacePage() {
  const { projectId = '' } = useParams()
  const { logout, session } = useAuth()
  const accountId = session?.userId ?? ''
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  const [projects, setProjects] = useState<Project[]>([])
  const [chapters, setChapters] = useState<ChapterMeta[]>([])
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([])
  const [candidateReferenceNames, setCandidateReferenceNames] = useState<Record<string, string>>({})
  const [selectedCid, setSelectedCid] = useState<string | null>(null)
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [batchTotal, setBatchTotal] = useState<number | null>(null)
  // 活动单章任务对应的章（§11 右栏按章过滤）：批次任务为 null（按 :ch{seq} 子线程切）
  const [activeChapterSeq, setActiveChapterSeq] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const [exporting, setExporting] = useState(false)
  const [deleting, setDeleting] = useState(false)
  // 放行本章（§6.11 确认流收尾）：awaiting_review 单章任务确认完候选后 resume 落库正文
  const [releaseTarget, setReleaseTarget] = useState<ReleaseTarget | null>(null)
  // 放行复用同一 task_id 续跑，SSE 需强制重连（useTaskEvents resumeKey 触发）
  const [releaseResumeKey, setReleaseResumeKey] = useState<number | null>(null)
  const [centerView, setCenterView] = useState<'plan' | 'write'>('write')
  const taskStartVersion = useRef(0)
  const chapterMaterializeVersion = useRef(0)
  const projectIdRef = useRef(projectId)
  const activeTaskIdRef = useRef<string | null>(null)
  const selectedCidRef = useRef<string | null>(null)
  const selectedChapterSeqRef = useRef<number | null>(null)
  const latestPlanArtifactRef = useRef<string | null>(null)
  const latestWriteArtifactRef = useRef<string | null>(null)
  projectIdRef.current = projectId
  // 只允许本页刚发起/续跑的任务在终态时自动跳章一次。历史任务恢复为 terminal 后
  // 不能持续把用户从其他章节拉回待确认章。
  const pendingAutoOpen = useRef<{ taskId: string; chapterSeq: number | null } | null>(null)

  useEffect(() => {
    activeTaskIdRef.current = activeTaskId
  }, [activeTaskId])

  useEffect(() => {
    if (selectedCidRef.current === selectedCid) return
    const selected = chapters.find((chapter) => chapter.id === selectedCid)
    const changedChapter = selectedChapterSeqRef.current !== (selected?.chapter_seq ?? null)
    selectedCidRef.current = selectedCid
    selectedChapterSeqRef.current = selected?.chapter_seq ?? null
    // 临时章节 id 被数据库真实 id 替换时仍是同一章，不能把正在展示的 Plan 翻回空正文。
    if (!changedChapter) return
    // 规划/写作中先回 Plan；Write 产物经 SSE 回放到达后再翻正文，避免切回来只看到空正文。
    setCenterView(selected?.status === 'planning' || selected?.status === 'writing' ? 'plan' : 'write')
    latestPlanArtifactRef.current = null
    latestWriteArtifactRef.current = null
  }, [chapters, selectedCid])

  // 候选池加载失败静默降级（空列表），不阻塞主链路。切书后丢弃上一本迟到的响应。
  const loadCandidates = useCallback(() => {
    const pid = projectId
    api.listCandidates(pid, '').then((list) => {
      if (projectIdRef.current === pid) setCandidates(list)
    }).catch(() => {
      if (projectIdRef.current === pid) setCandidates([])
    })
  }, [projectId])

  const loadCandidateReferences = useCallback(async () => {
    const pid = projectId
    const [graphResult, foreshadowResult] = await Promise.allSettled([
      api.listGraph(pid),
      api.listForeshadows(pid),
    ])
    if (projectIdRef.current !== pid) return
    const names: Record<string, string> = {}
    if (graphResult.status === 'fulfilled') {
      for (const node of graphResult.value.nodes) names[node.id] = node.name
    }
    if (foreshadowResult.status === 'fulfilled') {
      for (const item of foreshadowResult.value) {
        names[item.id] = item.description.length > 48 ? `${item.description.slice(0, 48)}…` : item.description
      }
    }
    setCandidateReferenceNames(names)
  }, [projectId])

  // 章序 → 待确认候选数（候选面板与章节列表角标联动）
  const pendingByChapter = useMemo(() => {
    const m = new Map<number, number>()
    for (const c of candidates.filter((item) => item.status === 'pending')) {
      m.set(c.source_chapter, (m.get(c.source_chapter) ?? 0) + 1)
    }
    return m
  }, [candidates])

  const loadProjects = useCallback(() => {
    api.listProjects().then((list) => {
      setProjects(list)
      const current = list.find((project) => project.id === projectIdRef.current)
      if (current && isProjectDraft(current)) navigate(projectHref(current), { replace: true })
    }).catch(() => setProjects([]))
  }, [navigate])

  const loadChapters = useCallback(async () => {
    const pid = projectId
    setError(null) // 新一次加载先清陈旧错误横幅
    try {
      const list = await api.listChapters(pid)
      if (projectIdRef.current !== pid) return []
      setChapters((current) => {
        const pending = current.filter((chapter) => (
          chapter.id.startsWith('pending-chapter:')
          && !list.some((item) => item.chapter_seq === chapter.chapter_seq)
        ))
        return [...list, ...pending].sort((a, b) => a.chapter_seq - b.chapter_seq)
      })
      return list
    } catch (err) {
      if (projectIdRef.current !== pid) return []
      setError(formatApiError(err, '章节加载失败'))
      return []
    }
  }, [projectId])

  useEffect(() => {
    // projectId 变化 = 切书：立刻丢掉上一本的章节/任务，避免迟到响应把 A 书画到 B 书上。
    setSelectedCid(null)
    setChapters([])
    setCandidates([])
    setCandidateReferenceNames({})
    setActiveTaskId(null)
    setBatchTotal(null)
    setActiveChapterSeq(null)
    setReleaseTarget(null)
    setReleaseResumeKey(null)
    setCenterView('write')
    selectedCidRef.current = null
    selectedChapterSeqRef.current = null
    latestPlanArtifactRef.current = null
    latestWriteArtifactRef.current = null
    chapterMaterializeVersion.current += 1
    pendingAutoOpen.current = null
    setError(null)
    loadProjects()
    const pid = projectId
    const saved = readActiveWrite(accountId, pid)
    if (saved) {
      // worker 接手前库里还没有 Task/章节。切回来必须先用本页记下的 taskId 接 SSE。
      const optimisticId = `pending-chapter:${saved.taskId}:${saved.chapterSeq}`
      const optimisticChapter: ChapterMeta = {
        id: optimisticId,
        chapter_seq: saved.chapterSeq,
        title: null,
        status: 'planning',
        word_count: 0,
        summary: null,
      }
      pendingAutoOpen.current = { taskId: saved.taskId, chapterSeq: saved.chapterSeq }
      // 同一次提交里 selectedSeq 仍是 null，后面的按章 listTasks effect 会先清 taskId。
      // ref / version 立刻对齐，避免空列表把刚恢复的写作盖掉。
      taskStartVersion.current += 1
      activeTaskIdRef.current = saved.taskId
      setActiveTaskId(saved.taskId)
      setBatchTotal(saved.batchTotal)
      setActiveChapterSeq(saved.chapterSeq)
      setCenterView('plan')
      setChapters([optimisticChapter])
      setSelectedCid(optimisticId)
      const materializeVersion = ++chapterMaterializeVersion.current
      void (async () => {
        for (let attempt = 0; attempt < 40; attempt += 1) {
          if (materializeVersion !== chapterMaterializeVersion.current) return
          try {
            const latest = await api.listChapters(pid)
            if (projectIdRef.current !== pid) return
            const target = latest.find((chapter) => chapter.chapter_seq === saved.chapterSeq)
            if (target) {
              setChapters(latest)
              setSelectedCid((current) => current === optimisticId ? target.id : current)
              return
            }
            setChapters((current) => {
              const pending = current.filter((chapter) => (
                chapter.id.startsWith('pending-chapter:')
                && !latest.some((item) => item.chapter_seq === chapter.chapter_seq)
              ))
              return [...latest, ...pending].sort((a, b) => a.chapter_seq - b.chapter_seq)
            })
          } catch {
            // 占位章仍由 SSE/终态刷新接管。
          }
          await new Promise((resolve) => window.setTimeout(resolve, 250))
        }
      })()
    }
    void loadChapters().then(async (list) => {
      if (projectIdRef.current !== pid) return
      if (saved) {
        const real = list.find((chapter) => chapter.chapter_seq === saved.chapterSeq)
        if (real) setSelectedCid(real.id)
        return
      }
      const inProgress = latestChapterAwaitingReview(list)
      if (inProgress) {
        setSelectedCid(inProgress.id)
        return
      }
      try {
        const tasks = await api.listTasks(pid)
        if (projectIdRef.current !== pid) return
        const active = latestActiveGenerationTask(tasks)
        const seq = active?.chapter_seq
        if (!active || !seq) return
        const existing = list.find((chapter) => chapter.chapter_seq === seq)
        if (existing) {
          setSelectedCid(existing.id)
          return
        }
        const optimisticId = `pending-chapter:${active.task_id}:${seq}`
        const optimisticChapter: ChapterMeta = {
          id: optimisticId,
          chapter_seq: seq,
          title: null,
          status: active.status === 'awaiting_plan' || active.status === 'queued' ? 'planning' : 'writing',
          word_count: 0,
          summary: null,
        }
        setChapters((current) => (
          current.some((chapter) => chapter.chapter_seq === seq)
            ? current
            : [...current, optimisticChapter].sort((a, b) => a.chapter_seq - b.chapter_seq)
        ))
        pendingAutoOpen.current = { taskId: active.task_id, chapterSeq: seq }
        setActiveTaskId(active.task_id)
        setBatchTotal(active.batch_size)
        setActiveChapterSeq(seq)
        setCenterView('plan')
        setSelectedCid(optimisticId)
      } catch {
        // 没有进行中任务就保持空书；不阻塞切书。
      }
    })
    loadCandidates()
    void loadCandidateReferences()
    return () => {
      chapterMaterializeVersion.current += 1
    }
  }, [accountId, loadProjects, loadChapters, loadCandidates, loadCandidateReferences, projectId])

  // 跨页深链（审计视图「跳章」→ /projects/:pid?chapter=<seq>）：一次性选中目标章并清参数
  useEffect(() => {
    const seqRaw = searchParams.get('chapter')
    if (seqRaw === null) return
    const target = chapters.find((c) => c.chapter_seq === Number(seqRaw))
    if (target) {
      setSelectedCid(target.id)
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, chapters, setSearchParams])

  const handleTaskStart = useCallback(
    (taskId: string, total?: number, chapterSeq?: number, _mode: WritingMode = 'auto') => {
      taskStartVersion.current += 1
      setBatchTotal(total ?? null)
      setActiveTaskId(taskId)
      setCenterView('plan')
      latestPlanArtifactRef.current = null
      latestWriteArtifactRef.current = null
      pendingAutoOpen.current = { taskId, chapterSeq: chapterSeq ?? null }
      if (chapterSeq !== undefined) {
        writeActiveWrite(accountId, projectId, { taskId, chapterSeq, batchTotal: total ?? null })
      }
      // 单章任务带出对应章（右栏按章过滤）；批次任务不带（按 :ch{seq} 子线程切）
      setActiveChapterSeq(chapterSeq ?? null)

      if (chapterSeq === undefined) return
      const existing = chapters.find((chapter) => chapter.chapter_seq === chapterSeq)
      if (existing) {
        setSelectedCid(existing.id)
        return
      }

      // 网关返回任务后先在界面建立目标章，确保新任务从第一帧起不占用上一章的正文和右栏。
      // worker 会随即持久化同章 writing 记录；轮询拿到真实 id 后无缝替换临时页。
      const optimisticId = `pending-chapter:${taskId}:${chapterSeq}`
      const optimisticChapter: ChapterMeta = {
        id: optimisticId,
        chapter_seq: chapterSeq,
        title: null,
        status: 'planning',
        word_count: 0,
        summary: null,
      }
      setChapters((current) => {
        if (current.some((chapter) => chapter.chapter_seq === chapterSeq)) return current
        return [...current, optimisticChapter].sort((a, b) => a.chapter_seq - b.chapter_seq)
      })
      setSelectedCid(optimisticId)

      const materializeVersion = ++chapterMaterializeVersion.current
      void (async () => {
        for (let attempt = 0; attempt < 40; attempt += 1) {
          if (materializeVersion !== chapterMaterializeVersion.current) return
          try {
            const list = await api.listChapters(projectId)
            const target = list.find((chapter) => chapter.chapter_seq === chapterSeq)
            if (target) {
              setChapters(list)
              setSelectedCid((current) => current === optimisticId ? target.id : current)
              return
            }
            setChapters([...list, optimisticChapter].sort((a, b) => a.chapter_seq - b.chapter_seq))
          } catch {
            // SSE/终态刷新仍会继续接管；短暂列表请求失败不打断正在运行的写作任务。
          }
          await new Promise((resolve) => window.setTimeout(resolve, 250))
        }
      })()
    },
    [accountId, chapters, projectId],
  )

  // 放行本章：resume 同一 task_id 续跑（§6.11 确认流收尾）。taskId 不变但 SSE 已关流，
  // 用 releaseResumeKey 强制重连；终态 → 既有 effect 刷章节/候选，正文出现、按钮消失。
  const handleRelease = useCallback((taskId: string, chapterSeq: number, batchSize?: number) => {
    taskStartVersion.current += 1
    setBatchTotal(batchSize ?? null)
    setActiveChapterSeq(chapterSeq)
    setActiveTaskId(taskId)
    pendingAutoOpen.current = { taskId, chapterSeq }
    setReleaseTarget(null)
    setReleaseResumeKey(Date.now())
  }, [])

  const handlePlanConfirmed = useCallback((taskId: string, chapterSeq: number) => {
    taskStartVersion.current += 1
    setBatchTotal(null)
    setActiveChapterSeq(chapterSeq)
    setActiveTaskId(taskId)
    setCenterView('write')
    pendingAutoOpen.current = { taskId, chapterSeq }
    setChapters((current) => current.map((chapter) => (
      chapter.chapter_seq === chapterSeq ? { ...chapter, status: 'writing' } : chapter
    )))
    setReleaseResumeKey(Date.now())
  }, [])

  const handlePlanCancelled = useCallback((_taskId: string, chapterSeq: number) => {
    pendingAutoOpen.current = null
    setChapters((current) => current.map((chapter) => (
      chapter.chapter_seq === chapterSeq ? { ...chapter, status: 'cancelled' } : chapter
    )))
    setReleaseResumeKey(Date.now())
    setCenterView('write')
    void loadChapters()
  }, [loadChapters])

  const task = useTaskEvents(
    activeTaskId,
    {
      ...(batchTotal ? { batchTotal } : {}),
      ...(releaseResumeKey !== null ? { resumeKey: releaseResumeKey } : {}),
    },
  )
  const taskPhase = task.phase

  // 生成任务进入终态/过期 → 章节状态与内容已更新：刷新列表 + 让编辑器重拉当前章正文 + 重载候选池
  useEffect(() => {
    if (taskPhase === 'terminal' || taskPhase === 'expired' || taskPhase === 'error') {
      chapterMaterializeVersion.current += 1
      void loadChapters()
      loadCandidates()
      setRefreshTick((t) => t + 1)
      // 单章任务停在 awaiting_review → 候选确认完后给「放行本章」；放行完成（done）即清
      if (task.status === 'awaiting_review' && activeTaskId && activeChapterSeq) {
        setReleaseTarget({ taskId: activeTaskId, chapterSeq: activeChapterSeq, batchSize: batchTotal ?? undefined })
      } else if (task.status === 'done' || task.status === 'failed' || task.status === 'cancelled') {
        clearActiveWrite(accountId, projectId)
        setReleaseTarget(null)
      }
    }
  }, [taskPhase, task.status, loadChapters, loadCandidates, activeTaskId, activeChapterSeq, batchTotal, accountId, projectId])

  // 单章完成或转人工后只自动打开一次刚生成的章。历史任务重载同样是 terminal，若不以
  // pendingAutoOpen 限定，用户从第 17 章点到其他章节时会立刻被旧终态 effect 拉回。
  useEffect(() => {
    const pending = pendingAutoOpen.current
    const target = chapterToOpenForPendingTask(
      chapters, pending, activeTaskId, taskPhase, selectedCid,
    )
    if (!target) return
    pendingAutoOpen.current = null
    if (target.id !== selectedCid) setSelectedCid(target.id)
  }, [taskPhase, activeTaskId, chapters, selectedCid])

  // 刷新页面或重新进入项目时恢复尚未处理的最新章节，让用户直接看到候选与放行入口。
  useEffect(() => {
    if (selectedCid || taskPhase !== 'idle') return
    const target = latestChapterAwaitingReview(chapters)
    if (target) setSelectedCid(target.id)
  }, [chapters, selectedCid, taskPhase])

  const selectedChapter = chapters.find((c) => c.id === selectedCid) ?? null

  // 兜底：刷新/重挂载后当前章若停在 awaiting_review（无活动任务流），从任务列表找回
  // 待人工任务补设放行目标。只设不清——放行完成由终态 effect 清、点按钮由 handleRelease
  // 清，此处清会在同一次提交里覆盖终态 effect 刚设的目标（selectedChapter.status 仍陈旧）。
  useEffect(() => {
    if (!selectedChapter || selectedChapter.status !== 'awaiting_review') return
    let cancelled = false
    api
      .listTasks(projectId, selectedChapter.chapter_seq)
      .then((tasks) => {
        if (cancelled) return
        const awaiting = tasks.find((t) => t.status === 'awaiting_review')
        if (awaiting) {
          setReleaseTarget({ taskId: awaiting.task_id, chapterSeq: selectedChapter.chapter_seq, batchSize: awaiting.batch_size ?? undefined })
        }
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [projectId, selectedChapter])

  // 右栏按章过滤（§11）：选中某章时，节点流转/花费只显示该章——批次按 :ch{seq}
  // 子线程切、单章按发起时带出的章对齐；未选中章则不过滤（时间线整体兜底）。
  const selectedSeq = selectedChapter?.chapter_seq ?? null

  // 每章只呈现一份状态流转：选章后加载覆盖该章的最新生成任务，实时和历史共用一条流程。
  useEffect(() => {
    const saved = readActiveWrite(accountId, projectId)
    const pending = pendingAutoOpen.current
    const remembered = pending?.taskId
      ? {
          taskId: pending.taskId,
          chapterSeq: pending.chapterSeq,
          batchTotal: saved?.taskId === pending.taskId ? saved.batchTotal : null,
        }
      : saved

    if (selectedSeq === null) {
      // 切书恢复的同一轮里章节还没选上；不能把刚接上的 taskId 清掉。
      if (remembered) return
      setActiveTaskId(null)
      setBatchTotal(null)
      setActiveChapterSeq(null)
      setReleaseTarget(null)
      return
    }
    if (remembered && remembered.chapterSeq === selectedSeq) {
      activeTaskIdRef.current = remembered.taskId
      setActiveTaskId(remembered.taskId)
      setBatchTotal(remembered.batchTotal ?? null)
      setActiveChapterSeq(selectedSeq)
      return
    }
    let cancelled = false
    setActiveTaskId(null)
    setBatchTotal(null)
    setActiveChapterSeq(null)
    setReleaseTarget(null)
    const requestVersion = taskStartVersion.current
    api.listTasks(projectId, selectedSeq)
      .then((tasks) => {
        if (cancelled || requestVersion !== taskStartVersion.current) return
        const latest = latestGenerationTask(tasks)
        if (!latest) {
          const keep = readActiveWrite(accountId, projectId)
          if (keep && keep.chapterSeq === selectedSeq) {
            activeTaskIdRef.current = keep.taskId
            setActiveTaskId(keep.taskId)
            setBatchTotal(keep.batchTotal)
            setActiveChapterSeq(selectedSeq)
            return
          }
          setActiveTaskId(null)
          setBatchTotal(null)
          setActiveChapterSeq(null)
          setReleaseTarget(null)
          return
        }
        setActiveTaskId(latest.task_id)
        setBatchTotal(latest.batch_size)
        setActiveChapterSeq(selectedSeq)
        setReleaseResumeKey(null)
        setReleaseTarget(latest.status === 'awaiting_review'
          ? { taskId: latest.task_id, chapterSeq: selectedSeq, batchSize: latest.batch_size ?? undefined }
          : null)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [accountId, projectId, selectedSeq])
  const taskRuns = runsForChapter(task.runs, {
    taskId: activeTaskId,
    batch: batchTotal !== null,
    selectedSeq,
    activeChapterSeq,
  })
  // 保留该任务的全部执行轮次；重进页面后 rewrite/replan 的前序审核也必须可追溯。
  const visibleTaskRuns = taskRuns
  const taskNodes = nodesForChapter(task.nodes, {
    taskId: activeTaskId,
    batch: batchTotal !== null,
    selectedSeq,
    activeChapterSeq,
  })
  const planArtifact = task.artifacts.find((item) => item.chapterSeq === selectedSeq && item.stage === 'plan') ?? null
  const writeArtifact = task.artifacts.find((item) => item.chapterSeq === selectedSeq && item.stage === 'write') ?? null
  const taskIsCreating = task.status === 'queued' || task.status === 'running' || task.status === 'awaiting_plan'
  const taskInFlight = Boolean(activeTaskId) && (
    taskIsCreating
    || task.status === 'awaiting_review'
    || taskPhase === 'connecting'
    || taskPhase === 'live'
    || taskPhase === 'reconnecting'
  )
  const hasPlan = planArtifact !== null || visibleTaskRuns.some((run) => (
    run.node === 'plan_chapter' && run.detail?.plan
  ))
  const isPendingChapter = selectedChapter?.id.startsWith('pending-chapter:') ?? false
  // 节点记录在 LLM 返回后才落库，写作进行中右栏会停在上一节点；用产物未完成态补一条实时步骤。
  // 仅任务在途时启用：终态/暂停下残留的未完成产物不该再显示「正在执行」。
  const liveNode = taskIsCreating
    ? liveStageNode(visibleTaskRuns, planArtifact, writeArtifact)
    : null
  const showCreationWorkspace = Boolean(selectedChapter && (hasPlan || taskIsCreating || isPendingChapter))
  const canOpenWrite = Boolean(
    selectedChapter && (
      writeArtifact !== null
      || selectedChapter.status === 'writing'
      || (!taskIsCreating && !isPendingChapter)
    ),
  )

  // 新的 Plan（包括审核后的 replan）先占满中栏；Writer 真正开始输出后再自动翻到正文。
  // artifact_id 作为一次产物的稳定边界，避免后续每个 delta 都抢走用户手动切换的页面。
  useEffect(() => {
    if (!planArtifact || latestPlanArtifactRef.current === planArtifact.artifactId) return
    latestPlanArtifactRef.current = planArtifact.artifactId
    setCenterView('plan')
  }, [planArtifact?.artifactId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!writeArtifact || latestWriteArtifactRef.current === writeArtifact.artifactId) return
    latestWriteArtifactRef.current = writeArtifact.artifactId
    setCenterView('write')
  }, [writeArtifact?.artifactId]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleNotFound = useCallback(() => {
    setSelectedCid(null)
    void loadChapters()
  }, [loadChapters])

  const handleSaved = useCallback(() => {
    void loadChapters()
  }, [loadChapters])

  // Markdown 导出（纯前端拼接下载）：逐章拉正文 → 标题/章节分隔 → Blob 下载
  const handleExportMarkdown = useCallback(async () => {
    if (exporting || chapters.length === 0) return
    setExporting(true)
    try {
      const project = projects.find((p) => p.id === projectId)
      const lines: string[] = [`# ${project?.title ?? 'Myink 作品'}`, '']
      for (const c of chapters) {
        const detail = await api.getChapter(projectId, c.id)
        const title = detail.title?.trim() || ''
        lines.push(title ? `## 第 ${c.chapter_seq} 章 · ${title}` : `## 第 ${c.chapter_seq} 章`, '')
        lines.push(detail.content?.trim() ?? '', '')
      }
      const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${project?.title ?? 'myink'}.md`
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      setError('导出失败')
    } finally {
      setExporting(false)
    }
  }, [exporting, chapters, projects, projectId])

  const handleNavigateChapter = useCallback(
    (seq: number) => {
      const target = chapters.find((c) => c.chapter_seq === seq)
      if (target) setSelectedCid(target.id)
    },
    [chapters],
  )

  // 整本书删除（阶段 6 硬删）：confirm → 删除成功回作品库；有进行中任务 → Python 409
  const handleDeleteBook = useCallback(() => {
    const project = projects.find((p) => p.id === projectId)
    const title = project?.title ?? '本书'
    if (!window.confirm(`删除《${title}》？全书正文、记忆、向量与任务记录将一并移除，不可撤销。`)) return
    setDeleting(true)
    api
      .deleteProject(projectId)
      .then(() => navigate('/projects'))
      .catch((err) => {
        setDeleting(false)
        setError(
          err instanceof ApiError && err.status === 409
            ? '本书有进行中任务，无法删除。请先暂停/取消后再试。'
            : err instanceof ApiError
              ? formatApiError(err, '删除失败')
              : '删除失败',
        )
      })
  }, [projects, projectId, navigate])

  return (
    <div className={styles.wrap}>
      <ProjectRail projects={projects} onLogout={logout} />

      <aside className={styles.chapters} aria-label="章节列表">
        <div className={styles.sideHead}>
          <h3 className={styles.sideTitle}>章节</h3>
          <button
            type="button"
            className="btn btn-quiet"
            disabled={exporting || chapters.length === 0}
            onClick={() => void handleExportMarkdown()}
          >
            {exporting ? '导出中…' : '导出 .md'}
          </button>
          <button
            type="button"
            className="btn btn-quiet"
            disabled={deleting}
            onClick={handleDeleteBook}
          >
            {deleting ? '删除中…' : '删除本书'}
          </button>
        </div>
        <ChapterList
          chapters={chapters}
          selectedCid={selectedCid}
          onSelect={setSelectedCid}
          pendingByChapter={pendingByChapter}
        />
      </aside>

      <main className={styles.main}>
        {error && <div className="banner banner-error">{error}</div>}
        {selectedChapter ? (
          showCreationWorkspace ? <div className={styles.creationWorkspace}>
            <nav className={styles.stageTabs} aria-label={`第 ${selectedChapter.chapter_seq} 章创作视图`}>
              <button
                type="button"
                className={centerView === 'plan' ? styles.stageTabActive : styles.stageTab}
                disabled={!hasPlan && !taskIsCreating && !isPendingChapter}
                aria-current={centerView === 'plan' ? 'page' : undefined}
                onClick={() => setCenterView('plan')}
              >
                <span>Plan</span><small>{task.status === 'awaiting_plan' ? '等待确认' : '章节计划'}</small>
              </button>
              <button
                type="button"
                className={centerView === 'write' ? styles.stageTabActive : styles.stageTab}
                disabled={!canOpenWrite}
                aria-current={centerView === 'write' ? 'page' : undefined}
                onClick={() => setCenterView('write')}
              >
                <span>正文</span><small>{writeArtifact?.complete ? '生成完成' : writeArtifact ? '实时写作' : 'Write'}</small>
              </button>
            </nav>
            <div className={styles.creationPage}>
              {centerView === 'plan' ? (
                <ChapterPlanPanel
                  key={`plan-${projectId}-${selectedChapter.chapter_seq}`}
                  taskId={activeTaskId}
                  chapterSeq={selectedChapter.chapter_seq}
                  status={task.status}
                  runs={visibleTaskRuns}
                  artifact={planArtifact}
                  onConfirmed={handlePlanConfirmed}
                  onCancelled={handlePlanCancelled}
                />
              ) : taskIsCreating ? (
                <StreamingChapterView
                  key={`write-${projectId}-${selectedChapter.chapter_seq}`}
                  chapterSeq={selectedChapter.chapter_seq}
                  artifact={writeArtifact}
                  summary={selectedChapter.summary}
                />
              ) : isPendingChapter ? (
                <div className={styles.generatingChapter} role="status">
                  <span className={styles.generatingBadge}>准备中</span>
                  <h2>第 {selectedChapter.chapter_seq} 章</h2>
                  <p>章节页面已经建立，正在连接生成任务。</p>
                </div>
              ) : (
                <ChapterEditor
                  projectId={projectId}
                  chapter={selectedChapter}
                  onNotFound={handleNotFound}
                  onSaved={handleSaved}
                  onMemoryChanged={loadCandidates}
                  refreshTick={refreshTick}
                />
              )}
            </div>
          </div> : (
            <ChapterEditor
              projectId={projectId}
              chapter={selectedChapter}
              onNotFound={handleNotFound}
              onSaved={handleSaved}
              onMemoryChanged={loadCandidates}
              refreshTick={refreshTick}
            />
          )
        ) : (
          <div className="empty">
            {chapters.length === 0 ? '还没有章节。在右侧发起首次生成。' : '选择左侧章节开始编辑。'}
          </div>
        )}
      </main>

      <aside className={styles.right} aria-label="生成与校验">
        <GenerationPanel
          projectId={projectId}
          chapters={chapters}
          selectedChapter={selectedChapter}
          taskBusy={taskInFlight}
          onTaskStart={handleTaskStart}
        />
        <TaskTimeline
          key={`flow-${projectId}-${selectedSeq ?? 'none'}`}
          taskId={activeTaskId}
          phase={task.phase}
          status={task.status}
          nodes={taskNodes}
          runs={visibleTaskRuns}
          liveNode={liveNode}
          progress={task.progress}
          chapterSeq={selectedSeq}
          error={task.error}
          onRetry={task.retry}
          canControl={batchTotal !== null}
          refresh={task.refresh}
        />
        <AuditPanel key={`audit-${projectId}-${selectedSeq ?? 'none'}`} runs={visibleTaskRuns} onNavigateChapter={handleNavigateChapter} />
        <CandidatePanel
          key={`${projectId}:${selectedSeq ?? 'none'}`}
          projectId={projectId}
          chapterSeq={selectedSeq}
          chapterStatus={selectedChapter?.status}
          candidates={candidates}
          onChanged={loadCandidates}
          releaseTarget={releaseTarget}
          onReleased={handleRelease}
          referenceNames={candidateReferenceNames}
        />
        <LessonsPanel projectId={projectId} />
      </aside>
    </div>
  )
}
