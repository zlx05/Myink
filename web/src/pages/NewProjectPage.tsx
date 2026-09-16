// 建书向导（§7.11）：书名/题材 + 一句话梗概 → 创建作品 → Planner 生成设定骨架草稿 →
// 可编辑确认 → 落库跳工作台。agent 只提案、用户确认是唯一 canon（§7.11 ③）。
// 布局复用 SettingsPage 的 wrap→rail→main→inner；分区编辑控件对齐 KeyField 风格。
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ProjectRail } from '../components/ProjectRail'
import { RankingsPanel } from '../components/RankingsPanel'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import { GenrePackFields } from '../components/GenrePackFields'
import {
  composeFields,
  displayGenre,
  emptyFields,
  groupCatalog,
  type GenreCatalogItem,
  type GenreFields,
} from '../lib/genrePacks'
import {
  emptySection,
  isSectionEmpty,
  sectionToBody,
  splitDraft,
  type SetupSection,
} from '../lib/bookDraft'
import type { BookOutline, OutlineStage, OutlineVolume, Project } from '../types'
import styles from './NewProjectPage.module.css'

/** 通用「每行一条」文本 ↔ 字符串数组（去空行） */
function linesToArray(text: string): string[] {
  return text
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
}

function arrayToLines(arr: string[]): string {
  return arr.join('\n')
}

function chapterRangeLabel(start?: number, end?: number): string {
  return start && end ? `第 ${start}–${end} 章` : ''
}

/** world_rules 键值编辑器：每行「键：值」（全角/半角冒号首个分隔），值可含冒号 */
function worldRulesToText(wr: Record<string, string>): string {
  return Object.entries(wr)
    .map(([k, v]) => `${k}：${v}`)
    .join('\n')
}

function textToWorldRules(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const s = line.trim()
    if (!s) continue
    const i = s.indexOf('：')
    const j = s.indexOf(':')
    const idx = i === -1 ? j : j === -1 ? i : Math.min(i, j)
    if (idx <= 0) continue
    out[s.slice(0, idx).trim()] = s.slice(idx + 1).trim()
  }
  return out
}

function ApiMessage(err: unknown, fallback: string): string {
  return formatApiError(err, fallback)
}

export default function NewProjectPage() {
  const { logout } = useAuth()
  const navigate = useNavigate()

  const [title, setTitle] = useState('')
  // 已落库的书名（AI 起名/用户填写后确认）；书名留空时由 Planner 在设定草稿带 title 建议
  const [savedTitle, setSavedTitle] = useState('')
  const [catalog, setCatalog] = useState<GenreCatalogItem[]>([])
  const [primaryId, setPrimaryId] = useState<string | null>(null)
  const [secondaryId, setSecondaryId] = useState<string | null>(null)
  const [genreFields, setGenreFields] = useState<GenreFields>(emptyFields())
  const [premise, setPremise] = useState('')
  // 每章目标字数（§6.9 三层字数控制；500–20000，默认 3000）
  const [targetWords, setTargetWords] = useState('3000')
  const [projects, setProjects] = useState<Project[]>([])
  const [pid, setPid] = useState<string | null>(null)
  const [section, setSection] = useState<SetupSection | null>(null)
  const [draftError, setDraftError] = useState<string | null>(null)
  // ③ 整书大纲（§11）：设定确认落库后出现；梗概 + 大致章节数 + 大致故事线 → Planner 提案
  const [setupConfirmed, setSetupConfirmed] = useState(false)
  const [outlineCount, setOutlineCount] = useState('200')
  const [outlineStoryline, setOutlineStoryline] = useState('')
  const [outline, setOutline] = useState<BookOutline | null>(null)
  const [outlineError, setOutlineError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [banner, setBanner] = useState<string | null>(null)
  const [ok, setOk] = useState<string | null>(null)

  useEffect(() => {
    void api.listGenrePacks().then(setCatalog).catch(() => {
      setBanner('题材目录加载失败，可先不选题材创建')
    })
  }, [])

  const primary = catalog.find((item) => item.id === primaryId) ?? null
  const secondary = catalog.find((item) => item.id === secondaryId) ?? null
  const genreLabel = displayGenre(primary, secondary)

  function pickPrimary(id: string) {
    const next = primaryId === id ? null : id
    setPrimaryId(next)
    setSecondaryId(null)
    const nextPrimary = catalog.find((item) => item.id === next) ?? null
    setGenreFields(composeFields(nextPrimary, null))
  }

  function pickSecondary(id: string) {
    if (!primaryId) return
    const next = secondaryId === id ? null : id
    setSecondaryId(next)
    const nextSecondary = catalog.find((item) => item.id === next) ?? null
    setGenreFields(composeFields(primary, nextSecondary))
  }

  async function createAndDraft() {
    const brief = premise.trim()
    if (!brief) {
      setBanner('请填写创作简报（设定与大纲的种子）')
      return
    }
    const words = Number(targetWords)
    if (!Number.isInteger(words) || words < 500 || words > 20000) {
      setBanner('目标字数需为 500–20000 的整数')
      return
    }
    const finalTitle = title.trim() || '未命名作品'
    setBusy('create')
    setBanner(null)
    setOk(null)
    try {
      const project = await api.createProject({
        title: finalTitle,
        primary_id: primaryId,
        secondary_id: secondaryId,
        genre_fields: genreFields,
        target_words: words,
      })
      setPid(project.id)
      setSavedTitle(finalTitle)
      setProjects(await api.listProjects())
      // InkOS 风格一键建书：设定草稿 + 整书大纲草稿并行生成（outline 只读 genre/premise，无需等设定确认）
      await Promise.all([regenerate(project.id), generateOutline(project.id)])
      setOk('作品已创建，设定与整书大纲草稿已生成，可编辑后确认')
    } catch (err) {
      setBanner(ApiMessage(err, '创建作品失败，请重试'))
    } finally {
      setBusy(null)
    }
  }

  async function regenerate(forPid: string) {
    setBusy('draft')
    setBanner(null)
    setDraftError(null)
    try {
      const resp = await api.setupDraft(forPid, premise.trim())
      setSection(splitDraft(resp.draft))
      setDraftError(resp.error)
      // AI 起名（InkOS --title 可选同款）：书名留空时 Planner 在设定草稿带 title 建议，创建后回写
      const aiTitle = typeof resp.draft.title === 'string' ? resp.draft.title.trim() : ''
      if (!title.trim() && aiTitle) {
        setTitle(aiTitle)
        setSavedTitle(aiTitle)
        try {
          await api.updateProject(forPid, { title: aiTitle })
        } catch {
          /* 标题是标签，回写失败静默（不阻塞草稿生成） */
        }
      }
    } catch (err) {
      setBanner(ApiMessage(err, '生成设定草稿失败，请重试'))
    } finally {
      setBusy(null)
    }
  }

  async function confirm() {
    if (!pid || !section) return
    setBusy('confirm')
    setBanner(null)
    setOk(null)
    try {
      await api.confirmSetup(pid, sectionToBody(section))
      await syncTitle()
      setSetupConfirmed(true)
      setOk('设定已确认落库，可继续检查整书大纲（第 ③ 步）')
    } catch (err) {
      setBanner(ApiMessage(err, '确认落库失败，请重试'))
    } finally {
      setBusy(null)
    }
  }

  // ③ 整书大纲：Planner 按目标章节数分卷提案 Objective + 卷 + 逐章；草稿不落库，确认后 PUT 整体替换。
  async function generateOutline(forPid: string) {
    const cc = Number(outlineCount)
    if (!Number.isInteger(cc) || cc < 50 || cc > 1000) {
      setBanner('大致章节数需为 50–1000 的整数')
      return
    }
    setBusy('outline-draft')
    setBanner(null)
    setOutlineError(null)
    try {
      const resp = await api.outlineDraft(forPid, {
        premise: premise.trim(),
        chapter_count: cc,
        storyline: outlineStoryline.trim(),
      })
      // 降级时 outline 为 {} → 归一为空结构，保证可编辑面板始终可渲染。
      setOutline({
        objective: typeof resp.outline.objective === 'string' ? resp.outline.objective : '',
        volumes: Array.isArray(resp.outline.volumes) ? resp.outline.volumes : [],
      })
      setOutlineError(resp.error)
    } catch (err) {
      setBanner(ApiMessage(err, '生成大纲失败，请重试'))
    } finally {
      setBusy(null)
    }
  }

  /** 输入框书名与已落库书名不同步时回写（输入框为 canon；AI 起名/手动改名后生效） */
  async function syncTitle() {
    if (!pid) return
    const t = title.trim()
    if (t && t !== savedTitle) {
      await api.updateProject(pid, { title: t })
      setSavedTitle(t)
    }
  }

  /** 大纲确认落库（单独确认 / 确认全部共用） */
  async function persistOutline() {
    if (!pid || !outline) return
    await api.confirmOutline(pid, {
      objective: outline.objective,
      volumes: outline.volumes.map((v) => ({
        title: v.title,
        theme: v.theme ?? '',
        goal: v.goal,
        key_results: v.key_results ?? [],
        end_event: v.end_event ?? '',
        chapter_start: v.chapter_start,
        chapter_end: v.chapter_end,
        stages: (v.stages ?? []).map((s) => ({
          name: s.name,
          chapter_start: s.chapter_start,
          chapter_end: s.chapter_end,
          goal: s.goal,
          beats: s.beats ?? [],
        })),
      })),
      premise: premise.trim(),
      chapter_count: Number(outlineCount) || 0,
      storyline: outlineStoryline.trim(),
    })
    await syncTitle()
  }

  async function confirmOutline() {
    if (!pid || !outline) return
    setBusy('outline-confirm')
    setBanner(null)
    setOk(null)
    try {
      await persistOutline()
      setOk('整书大纲已落库，进入工作台')
      navigate(`/projects/${pid}`)
    } catch (err) {
      setBanner(ApiMessage(err, '确认大纲失败，请重试'))
    } finally {
      setBusy(null)
    }
  }

  /** 确认全部并进入工作台（InkOS 风格一次落地）：设定未确认先落，再落大纲，再进入 */
  async function confirmAll() {
    if (!pid) return
    setBusy('confirm-all')
    setBanner(null)
    setOk(null)
    try {
      if (!setupConfirmed && section) {
        await api.confirmSetup(pid, sectionToBody(section))
        setSetupConfirmed(true)
      }
      if (outline) await persistOutline()
      await syncTitle()
      setOk('设定与整书大纲已落库，进入工作台')
      navigate(`/projects/${pid}`)
    } catch (err) {
      setBanner(ApiMessage(err, '确认失败，请重试'))
    } finally {
      setBusy(null)
    }
  }

  /** 暂不规划直接进入：先同步书名再跳转 */
  async function enterWorkspace() {
    if (!pid) return
    try {
      await syncTitle()
    } catch {
      /* 标题同步失败不阻塞进入 */
    }
    navigate(`/projects/${pid}`)
  }

  const updateOutline = (patch: Partial<BookOutline>) =>
    setOutline((o) => (o ? { ...o, ...patch } : o))

  const updateVolume = (vi: number, patch: Partial<OutlineVolume>) =>
    setOutline((o) =>
      o
        ? { ...o, volumes: o.volumes.map((v, j) => (j === vi ? { ...v, ...patch } : v)) }
        : o,
    )

  const updateStage = (vi: number, si: number, patch: Partial<OutlineStage>) =>
    setOutline((o) =>
      o
        ? {
            ...o,
            volumes: o.volumes.map((v, j) =>
              j === vi
                ? { ...v, stages: (v.stages ?? []).map((s, k) => (k === si ? { ...s, ...patch } : s)) }
                : v,
            ),
          }
        : o,
    )

  const removeStage = (vi: number, si: number) =>
    setOutline((o) =>
      o
        ? {
            ...o,
            volumes: o.volumes.map((v, j) =>
              j === vi ? { ...v, stages: (v.stages ?? []).filter((_, k) => k !== si) } : v,
            ),
          }
        : o,
    )

  const addStage = (vi: number) =>
    setOutline((o) =>
      o
        ? {
            ...o,
            volumes: o.volumes.map((v, j) =>
              j === vi
                ? { ...v, stages: [...(v.stages ?? []), { name: `第 ${(v.stages ?? []).length + 1} 段`, goal: '', beats: [] }] }
                : v,
            ),
          }
        : o,
    )

  const addVolume = () =>
    setOutline((o) =>
      o
        ? {
            ...o,
            volumes: [
              ...o.volumes,
              {
                title: `第 ${o.volumes.length + 1} 卷`,
                goal: '',
                stages: [{ name: '本卷', goal: '', beats: [] }],
              },
            ],
          }
        : o,
    )

  const updateSection = (patch: Partial<SetupSection>) =>
    setSection((s) => (s ? { ...s, ...patch } : s))

  const sec = section ?? emptySection()
  const emptyDraft = section !== null && isSectionEmpty(sec)

  return (
    <div className={styles.wrap}>
      <ProjectRail projects={projects} onLogout={logout} />
      <main className={styles.main}>
        <div className={styles.inner}>
          <header className={styles.header}>
            <div>
              <h1>新建作品</h1>
              <div className={styles.crumb}>创作简报启动，AI 生成设定骨架与整书大纲，你确认后落库。</div>
            </div>
          </header>

          {banner && <div className="banner banner-error">{banner}</div>}
          {ok && <div className="banner banner-warning">{ok}</div>}

          {/* 扫榜灵感（§10）：建书前的题材风向参考，全局端点；不注入任何生成节点 */}
          <RankingsPanel />

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>① 作品信息</h2>
            <label className={styles.field}>
              <span className={styles.fieldLabel}>书名</span>
              <input
                className="input"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="如《破晓录》，留空由 AI 起名"
                maxLength={60}
              />
            </label>
            <div className={styles.field}>
              <span className={styles.fieldLabel}>
                题材
                <span className={styles.hint}>（建书时选定，显示名锁定为包名；详情可改，只作用于即将创建的这本书）</span>
              </span>
              <div className={styles.lockedGenre}>{genreLabel}</div>
              {groupCatalog(catalog).map(({ group, items }) => (
                <div key={group} className={styles.genreGroup}>
                  <div className={styles.genreGroupTitle}>主题材 · {group}</div>
                  <div className={styles.genreChips}>
                    {items.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className={styles.chip + (primaryId === item.id ? ' ' + styles.chipOn : '')}
                        onClick={() => pickPrimary(item.id)}
                      >
                        {item.name}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              <div className={styles.genreGroup}>
                <div className={styles.genreGroupTitle}>辅题材（可选，须先选主题材）</div>
                {groupCatalog(catalog).map(({ group, items }) => (
                  <div key={`sec-${group}`}>
                    <div className={styles.genreSubTitle}>{group}</div>
                    <div className={styles.genreChips}>
                      {items.map((item) => (
                        <button
                          key={item.id}
                          type="button"
                          disabled={!primaryId || item.id === primaryId}
                          className={styles.chip + (secondaryId === item.id ? ' ' + styles.chipOn : '')}
                          onClick={() => pickSecondary(item.id)}
                        >
                          {item.name}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
              <details className={styles.genreDetails}>
                <summary>题材详情（默认折叠，可按自己的想法改）</summary>
                <GenrePackFields value={genreFields} onChange={setGenreFields} />
              </details>
            </div>
            <label className={styles.field}>
              <span className={styles.fieldLabel}>
                每章目标字数
                <span className={styles.hint}>（500–20000，驱动单章长度，默认 3000）</span>
              </span>
              <input
                className="input"
                type="number"
                min={500}
                max={20000}
                step={100}
                value={targetWords}
                onChange={(e) => setTargetWords(e.target.value)}
              />
            </label>
            <label className={styles.field}>
              <span className={styles.fieldLabel}>创作简报</span>
              <textarea
                className="textarea"
                rows={5}
                value={premise}
                onChange={(e) => setPremise(e.target.value)}
                placeholder={
                  '写你的脑洞，越具体越好：题材、主角身份、金手指、世界观、关键冲突或想要的结局。\n' +
                  '例：都市修仙，主角是个程序员，靠解析代码的方式理解修仙功法。\n' +
                  '或：被逐出宗门的外门弟子，带着一枚能推演因果的玉佩，从北境一路查清父母死因并证道。'
                }
              />
            </label>
            <div className={styles.saveRow}>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy !== null}
                onClick={createAndDraft}
              >
                {busy === 'create' ? '创建中…' : '创建作品'}
              </button>
            </div>
          </section>

          {pid && (
            <section className={`panel ${styles.section}`}>
              <div className={styles.draftHead}>
                <h2 className={styles.sectionTitle}>② 设定骨架草稿</h2>
                <button
                  type="button"
                  className="btn btn-quiet"
                  disabled={busy !== null}
                  onClick={() => regenerate(pid)}
                >
                  {busy === 'draft' ? '重新生成中…' : '重新生成草稿'}
                </button>
              </div>
              <p className={styles.hint}>
                由 Planner 按你的梗概生成（境界体系 / 世界观 / 硬约束 / 势力 / 核心人物 / 关键地点）。
                草稿只是提案，可直接编辑，确认后落库生效。
              </p>
              {draftError && (
                <div className="banner banner-warning">LLM 生成降级：{draftError}（可手填后确认）</div>
              )}
              {section === null ? (
                <div className="empty">正在生成设定草稿…</div>
              ) : emptyDraft ? (
                <div className="empty">草稿为空（LLM 未产出），请手动填写以下分区。</div>
              ) : null}

              {section !== null && (
                <div>
                  <div className={styles.block}>
                    <span className={styles.fieldLabel}>境界体系（每行一阶）</span>
                    <textarea
                      className="textarea"
                      rows={Math.max(3, sec.realm_order.length)}
                      value={arrayToLines(sec.realm_order)}
                      onChange={(e) => updateSection({ realm_order: linesToArray(e.target.value) })}
                    />
                  </div>
                  <div className={styles.block}>
                    <span className={styles.fieldLabel}>世界观规则（每行「键：值」）</span>
                    <textarea
                      className="textarea"
                      rows={Math.max(3, Object.keys(sec.world_rules).length)}
                      value={worldRulesToText(sec.world_rules)}
                      onChange={(e) => updateSection({ world_rules: textToWorldRules(e.target.value) })}
                    />
                  </div>
                  <div className={styles.block}>
                    <span className={styles.fieldLabel}>硬约束（每行一条）</span>
                    <textarea
                      className="textarea"
                      rows={Math.max(3, sec.hard_constraints.length)}
                      value={arrayToLines(sec.hard_constraints)}
                      onChange={(e) => updateSection({ hard_constraints: linesToArray(e.target.value) })}
                    />
                  </div>

                  <div className={styles.block}>
                    <span className={styles.fieldLabel}>势力</span>
                    {sec.forces.map((f, i) => (
                      <div key={i} className={styles.rowGrid}>
                        <input
                          className="input"
                          placeholder="名称"
                          value={f.name}
                          onChange={(e) =>
                            updateSection({
                              forces: sec.forces.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)),
                            })
                          }
                        />
                        <input
                          className="input"
                          placeholder="立场"
                          value={f.stance}
                          onChange={(e) =>
                            updateSection({
                              forces: sec.forces.map((x, j) =>
                                j === i ? { ...x, stance: e.target.value } : x,
                              ),
                            })
                          }
                        />
                        <button
                          type="button"
                          className="btn btn-quiet"
                          onClick={() =>
                            updateSection({ forces: sec.forces.filter((_, j) => j !== i) })
                          }
                        >
                          删
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="btn btn-quiet"
                      onClick={() =>
                        updateSection({ forces: [...sec.forces, { name: '', stance: '', resources: [] }] })
                      }
                    >
                      + 添加势力
                    </button>
                  </div>

                  <div className={styles.block}>
                    <span className={styles.fieldLabel}>核心人物</span>
                    {sec.characters.map((c, i) => (
                      <div key={i} className={styles.charGrid}>
                        <input
                          className="input"
                          placeholder="姓名"
                          value={c.name}
                          onChange={(e) =>
                            updateSection({
                              characters: sec.characters.map((x, j) =>
                                j === i ? { ...x, name: e.target.value } : x,
                              ),
                            })
                          }
                        />
                        <input
                          className="input"
                          placeholder="境界上限"
                          value={c.realm_cap}
                          onChange={(e) =>
                            updateSection({
                              characters: sec.characters.map((x, j) =>
                                j === i ? { ...x, realm_cap: e.target.value } : x,
                              ),
                            })
                          }
                        />
                        <input
                          className="input"
                          placeholder="出身"
                          value={c.origin}
                          onChange={(e) =>
                            updateSection({
                              characters: sec.characters.map((x, j) =>
                                j === i ? { ...x, origin: e.target.value } : x,
                              ),
                            })
                          }
                        />
                        <button
                          type="button"
                          className="btn btn-quiet"
                          onClick={() =>
                            updateSection({ characters: sec.characters.filter((_, j) => j !== i) })
                          }
                        >
                          删
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="btn btn-quiet"
                      onClick={() =>
                        updateSection({
                          characters: [
                            ...sec.characters,
                            { name: '', role: '', race: '', origin: '', realm_cap: '', personality: '' },
                          ],
                        })
                      }
                    >
                      + 添加人物
                    </button>
                  </div>

                  <div className={styles.block}>
                    <span className={styles.fieldLabel}>关键地点</span>
                    {sec.locations.map((l, i) => (
                      <div key={i} className={styles.rowGrid}>
                        <input
                          className="input"
                          placeholder="名称"
                          value={l.name}
                          onChange={(e) =>
                            updateSection({
                              locations: sec.locations.map((x, j) =>
                                j === i ? { name: e.target.value } : x,
                              ),
                            })
                          }
                        />
                        <button
                          type="button"
                          className="btn btn-quiet"
                          onClick={() =>
                            updateSection({ locations: sec.locations.filter((_, j) => j !== i) })
                          }
                        >
                          删
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="btn btn-quiet"
                      onClick={() => updateSection({ locations: [...sec.locations, { name: '' }] })}
                    >
                      + 添加地点
                    </button>
                  </div>

                  <div className={styles.saveRow}>
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={busy !== null}
                      onClick={confirm}
                    >
                      {busy === 'confirm' ? '确认中…' : '确认落库'}
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}

          {pid && (
            <section className={`panel ${styles.section}`}>
              <div className={styles.draftHead}>
                <h2 className={styles.sectionTitle}>③ 整书大纲</h2>
                <button
                  type="button"
                  className="btn btn-quiet"
                  disabled={busy !== null}
                  onClick={() => pid && generateOutline(pid)}
                >
                  {busy === 'outline-draft' ? '生成中…' : outline ? '重新生成大纲' : '生成大纲'}
                </button>
              </div>
              <p className={styles.hint}>
                按题材节奏分卷（推进快则卷多），只规划到卷和约每 30 章一段的阶段，不写逐章细纲。
                写作时注入当前卷目标与当前阶段。可编辑后确认。
              </p>
              {outlineError && (
                <div className="banner banner-warning">LLM 生成降级：{outlineError}（可手填后确认）</div>
              )}
              <div className={styles.block}>
                <span className={styles.fieldLabel}>大致章节数（50–1000）</span>
                <input
                  className="input"
                  type="number"
                  min={50}
                  max={1000}
                  value={outlineCount}
                  onChange={(e) => setOutlineCount(e.target.value)}
                />
              </div>
              <details className={styles.block}>
                <summary className={styles.fieldLabel}>
                  补充故事线（可选，不填则由 Planner 自动推导）
                </summary>
                <textarea
                  className="textarea"
                  rows={2}
                  value={outlineStoryline}
                  onChange={(e) => setOutlineStoryline(e.target.value)}
                  placeholder="如：前期入宗立身，中期追查玉佩真相，后期宗门惊变、决战北境。"
                />
              </details>
              {outline && (
                <div className={styles.outlineStack}>
                  <div className={styles.block}>
                    <span className={styles.fieldLabel}>全书 Objective（终局，可验证状态）</span>
                    <textarea
                      className={`textarea ${styles.growArea}`}
                      rows={4}
                      value={outline.objective}
                      onChange={(e) => updateOutline({ objective: e.target.value })}
                      placeholder="如：从杂役修士成为宗门长老并公开父辈冤案真相"
                    />
                  </div>
                  {outline.volumes.map((v, vi) => {
                    const volumeRange = chapterRangeLabel(v.chapter_start, v.chapter_end)
                    const stageCount = (v.stages ?? []).length
                    return (
                      <details key={vi} className={styles.volumeFold}>
                        <summary className={styles.foldSummary}>
                          <span className={styles.foldTitle}>
                            第 {vi + 1} 卷大纲
                            {v.title ? ` · ${v.title}` : ''}
                          </span>
                          <span className={styles.foldMeta}>
                            {volumeRange || '章区间未定'}
                            {stageCount ? ` · ${stageCount} 段` : ''}
                          </span>
                        </summary>
                        <div className={styles.foldBody}>
                          <label className={styles.field}>
                            <span className={styles.fieldLabel}>卷名</span>
                            <input
                              className="input"
                              placeholder="卷名"
                              value={v.title}
                              onChange={(e) => updateVolume(vi, { title: e.target.value })}
                            />
                          </label>
                          <label className={styles.field}>
                            <span className={styles.fieldLabel}>主题</span>
                            <input
                              className="input"
                              value={v.theme ?? ''}
                              placeholder="一句话主题"
                              onChange={(e) => updateVolume(vi, { theme: e.target.value })}
                            />
                          </label>
                          <div className={styles.rangeRow}>
                            <label className={styles.field}>
                              <span className={styles.fieldLabel}>起始章</span>
                              <input
                                className="input"
                                type="number"
                                min={1}
                                placeholder="起始章"
                                value={v.chapter_start ?? ''}
                                onChange={(e) =>
                                  updateVolume(vi, { chapter_start: Number(e.target.value) || undefined })
                                }
                              />
                            </label>
                            <label className={styles.field}>
                              <span className={styles.fieldLabel}>结束章</span>
                              <input
                                className="input"
                                type="number"
                                min={1}
                                placeholder="结束章"
                                value={v.chapter_end ?? ''}
                                onChange={(e) =>
                                  updateVolume(vi, { chapter_end: Number(e.target.value) || undefined })
                                }
                              />
                            </label>
                          </div>
                          <label className={styles.field}>
                            <span className={styles.fieldLabel}>卷目标</span>
                            <textarea
                              className={`textarea ${styles.growArea}`}
                              rows={3}
                              placeholder="本卷结束时主角须达到的可验证状态"
                              value={v.goal}
                              onChange={(e) => updateVolume(vi, { goal: e.target.value })}
                            />
                          </label>
                          <label className={styles.field}>
                            <span className={styles.fieldLabel}>关键结果 KR（每行一条）</span>
                            <textarea
                              className={`textarea ${styles.growArea}`}
                              rows={4}
                              placeholder="每行一条可验证结果"
                              value={arrayToLines(v.key_results ?? [])}
                              onChange={(e) => updateVolume(vi, { key_results: linesToArray(e.target.value) })}
                            />
                          </label>
                          <label className={styles.field}>
                            <span className={styles.fieldLabel}>卷末不可逆事件</span>
                            <textarea
                              className={`textarea ${styles.growArea}`}
                              rows={3}
                              placeholder="只写事件，不写第几章"
                              value={v.end_event ?? ''}
                              onChange={(e) => updateVolume(vi, { end_event: e.target.value })}
                            />
                          </label>
                          {(v.stages ?? []).map((s, si) => {
                            const stageRange = chapterRangeLabel(s.chapter_start, s.chapter_end)
                            return (
                              <details key={si} className={styles.stageFold}>
                                <summary className={styles.foldSummary}>
                                  <span className={styles.foldTitle}>
                                    {s.name || `第 ${si + 1} 段`}
                                  </span>
                                  <span className={styles.foldMeta}>{stageRange || '章区间未定'}</span>
                                </summary>
                                <div className={styles.foldBody}>
                                  <div className={styles.stageHead}>
                                    <label className={styles.field}>
                                      <span className={styles.fieldLabel}>阶段名</span>
                                      <input
                                        className="input"
                                        placeholder="前期 / 中期 / 后期"
                                        value={s.name}
                                        onChange={(e) => updateStage(vi, si, { name: e.target.value })}
                                      />
                                    </label>
                                    <button
                                      type="button"
                                      className="btn btn-quiet"
                                      onClick={() => removeStage(vi, si)}
                                    >
                                      删除本段
                                    </button>
                                  </div>
                                  <div className={styles.rangeRow}>
                                    <label className={styles.field}>
                                      <span className={styles.fieldLabel}>起始章</span>
                                      <input
                                        className="input"
                                        type="number"
                                        min={1}
                                        placeholder="起始章"
                                        value={s.chapter_start ?? ''}
                                        onChange={(e) =>
                                          updateStage(vi, si, {
                                            chapter_start: Number(e.target.value) || undefined,
                                          })
                                        }
                                      />
                                    </label>
                                    <label className={styles.field}>
                                      <span className={styles.fieldLabel}>结束章</span>
                                      <input
                                        className="input"
                                        type="number"
                                        min={1}
                                        placeholder="结束章"
                                        value={s.chapter_end ?? ''}
                                        onChange={(e) =>
                                          updateStage(vi, si, {
                                            chapter_end: Number(e.target.value) || undefined,
                                          })
                                        }
                                      />
                                    </label>
                                  </div>
                                  <label className={styles.field}>
                                    <span className={styles.fieldLabel}>阶段目标</span>
                                    <textarea
                                      className={`textarea ${styles.growArea}`}
                                      rows={3}
                                      placeholder="约 30 章一段，写作时会注入当前阶段"
                                      value={s.goal}
                                      onChange={(e) => updateStage(vi, si, { goal: e.target.value })}
                                    />
                                  </label>
                                  <label className={styles.field}>
                                    <span className={styles.fieldLabel}>阶段节拍（每行一条）</span>
                                    <textarea
                                      className={`textarea ${styles.growArea}`}
                                      rows={Math.max(4, s.beats?.length ?? 0)}
                                      placeholder="谁 + 在何处 + 做什么 + 导致什么"
                                      value={arrayToLines(s.beats ?? [])}
                                      onChange={(e) =>
                                        updateStage(vi, si, { beats: linesToArray(e.target.value) })
                                      }
                                    />
                                  </label>
                                </div>
                              </details>
                            )
                          })}
                          <button type="button" className="btn btn-quiet" onClick={() => addStage(vi)}>
                            + 本卷加一段
                          </button>
                        </div>
                      </details>
                    )
                  })}
                  <div className={styles.saveRow}>
                    <button type="button" className="btn btn-quiet" disabled={busy !== null} onClick={addVolume}>
                      + 新增一卷
                    </button>
                    <button
                      type="button"
                      className="btn btn-quiet"
                      disabled={busy !== null}
                      onClick={confirmOutline}
                    >
                      {busy === 'outline-confirm' ? '确认中…' : '仅确认大纲并进入'}
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={busy !== null}
                      onClick={confirmAll}
                    >
                      {busy === 'confirm-all' ? '确认中…' : '确认设定与大纲并进入工作台'}
                    </button>
                    <button
                      type="button"
                      className="btn btn-quiet"
                      disabled={busy !== null}
                      onClick={enterWorkspace}
                    >
                      暂不规划，直接进入
                    </button>
                  </div>
                </div>
              )}
              {!outline && (
                <div className={styles.saveRow}>
                  <button
                    type="button"
                    className="btn btn-quiet"
                    disabled={busy !== null}
                    onClick={enterWorkspace}
                  >
                    暂不规划，直接进入工作台
                  </button>
                </div>
              )}
            </section>
          )}
        </div>
      </main>
    </div>
  )
}
