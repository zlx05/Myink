// 设定浏览页（§7.11）：人物卡片（静态基底 + 当前状态台账）+ 世界观（realm_order 阶段箭头）+
// 硬约束 + 势力/地点。数据 GET world + GET characters 并行；无设定 → 空态不报错。
import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ProjectRail } from '../components/ProjectRail'
import { WorldGraph } from '../components/WorldGraph'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import type {
  BookOutline,
  ChapterMeta,
  CharacterCard,
  Foreshadow,
  LoreEntity,
  Project,
  WorldView,
} from '../types'
import styles from './LorePage.module.css'

// 设定实体分组（§7.11 ④ 自动建档：正文抽取低风险自动登记）
const ENTITY_TYPE_LABELS: Record<string, string> = {
  item: '物品/武器',
  skill: '功法/技能',
  location: '地点',
}
const ENTITY_TYPE_ORDER = ['item', 'skill', 'location']

// 人物卡片基础属性中文标签（character_card payload 的 identity/role/importance）
const BASE_ATTR_LABELS: Record<string, string> = {
  identity: '身份',
  role: '角色定位',
  importance: '重要性',
}

// 当前状态台账字段中文标签（character_states.field 枚举：realm/injury/goal/knowledge 等 9 类）
const STATE_LABELS: Record<string, string> = {
  realm: '修为',
  power: '实力',
  injury: '伤势',
  goal: '目标',
  location: '位置',
  item: '随身物品',
  knowledge: '已知信息',
  identity: '身份',
  alive: '生死',
}

// 伏笔状态机中文标签（§7.9：planted/developing/resolved/dropped）
const FORESHADOW_STATUS_LABELS: Record<string, string> = {
  planted: '种植中',
  developing: '推进中',
  resolved: '已回收',
  dropped: '已废弃',
}
const FORESHADOW_STATUS_ORDER = ['planted', 'developing', 'resolved', 'dropped']

/** realm_order 渲染为阶段箭头列表（对齐 seed world_rules.realm_order 口径） */
function RealmOrder({ value }: { value: unknown }) {
  const list = Array.isArray(value) ? value.map((x) => String(x)).filter(Boolean) : []
  if (list.length === 0) return null
  return (
    <div className={styles.realmOrder}>
      {list.map((r, i) => (
        <span key={i} className={styles.realmStep}>
          <span className={styles.realmItem}>{r}</span>
          {i < list.length - 1 && <span className={styles.realmArrow}>→</span>}
        </span>
      ))}
    </div>
  )
}

/** 世界观键值（跳过 realm_order——已单独渲染） */
function WorldRules({ world_rules }: { world_rules: Record<string, unknown> }) {
  const entries = Object.entries(world_rules).filter(([k]) => k !== 'realm_order')
  if (entries.length === 0) return <div className="empty">暂无世界观规则。</div>
  return (
    <dl className={styles.ruleList}>
      {entries.map(([k, v]) => (
        <div key={k} className={styles.ruleRow}>
          <dt className={styles.ruleKey}>{k}</dt>
          <dd className={styles.ruleVal}>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
        </div>
      ))}
    </dl>
  )
}

function CharacterCardBlock({
  card,
  expanded,
  onToggle,
}: {
  card: CharacterCard
  expanded: boolean
  onToggle: () => void
}) {
  const stateRows = Object.entries(card.state)
  return (
    <article className={`panel ${styles.card}`}>
      <button type="button" className={styles.cardHead} onClick={onToggle}>
        <span className={styles.cardName}>{card.name}</span>
        <span className="badge badge-accent">{card.realm_cap}</span>
        {card.race && <span className="badge">{card.race}</span>}
        <span className={styles.cardArrow}>{expanded ? '▾' : '▸'}</span>
      </button>
      {expanded && (
        <div className={styles.cardBody}>
          <dl className={styles.cardMeta}>
            {card.origin && (
              <>
                <dt>出身</dt>
                <dd>{card.origin}</dd>
              </>
            )}
            {card.personality && (
              <>
                <dt>性格</dt>
                <dd>{card.personality}</dd>
              </>
            )}
          </dl>
          {Object.keys(card.base_attrs).length > 0 && (
            <>
              <span className={styles.cardLabel}>基础属性</span>
              <dl className={styles.cardMeta}>
                {Object.entries(card.base_attrs).map(([k, v]) => (
                  <Fragment key={k}>
                    <dt>{BASE_ATTR_LABELS[k] ?? k}</dt>
                    <dd>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
                  </Fragment>
                ))}
              </dl>
            </>
          )}
          <span className={styles.cardLabel}>当前状态</span>
          {stateRows.length === 0 ? (
            <div className="empty">暂无状态记录。</div>
          ) : (
            <dl className={styles.cardMeta}>
              {stateRows.map(([k, v]) => (
                <Fragment key={k}>
                  <dt>{STATE_LABELS[k] ?? k}</dt>
                  <dd>{v}</dd>
                </Fragment>
              ))}
            </dl>
          )}
        </div>
      )}
    </article>
  )
}

export default function LorePage() {
  const { projectId = '' } = useParams()
  const { logout } = useAuth()

  const [projects, setProjects] = useState<Project[]>([])
  const [world, setWorld] = useState<WorldView | null>(null)
  const [characters, setCharacters] = useState<CharacterCard[]>([])
  const [entities, setEntities] = useState<LoreEntity[]>([])
  const [foreshadows, setForeshadows] = useState<Foreshadow[]>([])
  const [chapters, setChapters] = useState<ChapterMeta[]>([])
  const [outline, setOutline] = useState<BookOutline | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [banner, setBanner] = useState<string | null>(null)

  const load = useCallback(async () => {
    setBanner(null)
    try {
      const [proj, w, ch, ent, fo, chaps, ol] = await Promise.all([
        api.listProjects(),
        api.getWorld(projectId),
        api.getCharacters(projectId),
        api.listEntities(projectId),
        api.listForeshadows(projectId),
        api.listChapters(projectId),
        api.getOutline(projectId),
      ])
      setProjects(proj)
      setWorld(w)
      setCharacters(ch)
      setEntities(ent)
      setForeshadows(fo)
      setChapters(chaps)
      setOutline(ol.outline)
    } catch (err) {
      setBanner(formatApiError(err, '设定加载失败'))
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div className={styles.wrap}>
      <ProjectRail projects={projects} onLogout={logout} />
      <main className={styles.main}>
        <div className={styles.inner}>
          <header className={styles.header}>
            <div>
              <h1>设定</h1>
              <div className={styles.crumb}>
                <Link to={`/projects/${projectId}`}>返回工作台</Link>
              </div>
            </div>
          </header>

          {banner && <div className="banner banner-error">{banner}</div>}
          {!world && !banner && <div className="empty">加载中…</div>}

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>整书大纲</h2>
            <p className={styles.hint}>
              建书第 ③ 步规划的「全书 Objective → 卷 → 约 30 章一段的阶段」。
              写作注入当前卷目标与当前阶段。
            </p>
            {outline == null ? (
              <div className="empty">暂无大纲。可在「新建作品」第 ③ 步规划整书卷与阶段。</div>
            ) : (
              <div>
                {outline.objective && (
                  <>
                    <span className={styles.cardLabel}>全书 Objective（终局）</span>
                    <p className={styles.entityDesc}>{outline.objective}</p>
                  </>
                )}
                {outline.volumes.length === 0 ? (
                  <div className="empty">大纲暂无卷。</div>
                ) : (
                  <div className={styles.outlineStack}>
                  {outline.volumes.map((v, vi) => (
                    <details key={vi} className={styles.volumeFold}>
                      <summary className={styles.foldSummary}>
                        <span className={styles.foldTitle}>
                          第 {v.volume_seq ?? vi + 1} 卷大纲
                          {v.title ? ` · ${v.title}` : ''}
                        </span>
                        <span className={styles.foldMeta}>
                          {v.chapter_start && v.chapter_end
                            ? `第 ${v.chapter_start}–${v.chapter_end} 章`
                            : '章区间未定'}
                          {(v.stages?.length ?? 0) > 0 ? ` · ${v.stages?.length} 段` : ''}
                        </span>
                      </summary>
                      <div className={styles.foldBody}>
                      {v.theme && <span className="badge">{v.theme}</span>}
                      {v.goal && <span className={styles.entityDesc}>卷目标：{v.goal}</span>}
                      {v.key_results && v.key_results.length > 0 && (
                        <ul className={styles.constraintList}>
                          {v.key_results.map((k, i) => (
                            <li key={i}>KR：{k}</li>
                          ))}
                        </ul>
                      )}
                      {v.end_event && <span className={styles.entityDesc}>卷末事件：{v.end_event}</span>}
                      {!(v.stages && v.stages.length) ? (
                        <div className="empty">本卷暂无阶段。</div>
                      ) : (
                        v.stages.map((s, si) => (
                          <details key={s.stage_seq ?? si} className={styles.stageFold}>
                            <summary className={styles.foldSummary}>
                              <span className={styles.foldTitle}>{s.name || `第 ${si + 1} 段`}</span>
                              <span className={styles.foldMeta}>
                                {s.chapter_start && s.chapter_end
                                  ? `第 ${s.chapter_start}–${s.chapter_end} 章`
                                  : '章区间未定'}
                              </span>
                            </summary>
                            {s.goal && (
                              <p className={`${styles.entityDesc} ${styles.foldBody}`}>{s.goal}</p>
                            )}
                          </details>
                        ))
                      )}
                      </div>
                    </details>
                  ))}
                  </div>
                )}
              </div>
            )}
          </section>

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>人物卡片</h2>
            {characters.length === 0 ? (
              <div className="empty">暂无人物。可在「新建作品」设定或后续章节抽取中建立。</div>
            ) : (
              <div className={styles.cardGrid}>
                {characters.map((c) => (
                  <CharacterCardBlock
                    key={c.id}
                    card={c}
                    expanded={expanded.has(c.id)}
                    onToggle={() => toggle(c.id)}
                  />
                ))}
              </div>
            )}
          </section>

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>章节记忆</h2>
            <p className={styles.hint}>
              各章摘要（§7 短期记忆）：每章正文落库时随事件沉淀，回溯「前面章节发生了什么」。
            </p>
            {chapters.length === 0 ? (
              <div className="empty">暂无已写章节。开始写作后各章摘要自动沉淀于此。</div>
            ) : (
              <ul className={styles.entityList}>
                {[...chapters]
                  .sort((a, b) => b.chapter_seq - a.chapter_seq)
                  .map((c) => (
                    <li key={c.id} className={styles.entityItem}>
                      <span className={styles.entityHead}>
                        <span className={styles.entityName}>第 {c.chapter_seq} 章</span>
                        <span className="badge">{c.status}</span>
                        {c.word_count != null && (
                          <span className={styles.entityDesc}>{c.word_count} 字</span>
                        )}
                      </span>
                      {c.summary ? (
                        <span className={styles.entityDesc}>{c.summary}</span>
                      ) : (
                        <span className={styles.entityDesc}>（本章暂无摘要）</span>
                      )}
                    </li>
                  ))}
              </ul>
            )}
          </section>

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>关系图谱</h2>
            <p className={styles.hint}>
              全量世界拓扑（人物 / 势力 / 地点 / 设定实体 4 类节点；人物关系与地点层级为边）。
              实线为活跃关系，虚线为已失效关系；可拖拽、滚轮缩放、点图例过滤分类。
            </p>
            <WorldGraph projectId={projectId} />
          </section>

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>设定实体</h2>
            <p className={styles.hint}>
              正文中首次明确命名的武器 / 功法 / 技能 / 地点自动登记（§7.11 ④ 低风险自动建档），
              确认新人物卡片时同步写入。
            </p>
            {entities.length === 0 ? (
              <div className="empty">暂无设定实体。写作中出现新武器 / 功法 / 技能 / 地点时自动登记。</div>
            ) : (
              <div className={styles.twoCol}>
                {ENTITY_TYPE_ORDER.map((t) => {
                  const list = entities.filter((e) => e.entity_type === t)
                  return (
                    <div key={t}>
                      <h3 className={styles.subTitle}>{ENTITY_TYPE_LABELS[t] ?? t}</h3>
                      {list.length === 0 ? (
                        <div className="empty">暂无</div>
                      ) : (
                        <ul className={styles.entityList}>
                          {list.map((e) => (
                            <li key={e.id} className={styles.entityItem}>
                              <span className={styles.entityHead}>
                                <span className={styles.entityName}>{e.name}</span>
                                {e.first_seen_chapter != null && (
                                  <span className="badge">第 {e.first_seen_chapter} 章</span>
                                )}
                              </span>
                              {e.description && (
                                <span className={styles.entityDesc}>{e.description}</span>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </section>

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>伏笔池</h2>
            <p className={styles.hint}>
              伏笔台账（§7.9 状态机）：种植中/推进中的开放伏笔会被后续章节规划消费
              （决定回收/延续/放弃），已回收/已废弃保留历史供复盘。
            </p>
            {foreshadows.length === 0 ? (
              <div className="empty">暂无伏笔。正文中出现可回收的悬念/物件/承诺时自动登记。</div>
            ) : (
              <ul className={styles.entityList}>
                {FORESHADOW_STATUS_ORDER.flatMap((s) =>
                  foreshadows
                    .filter((f) => f.status === s)
                    .map((f) => (
                      <li key={f.id} className={styles.entityItem}>
                        <span className={styles.entityHead}>
                          <span className="badge badge-accent">
                            {FORESHADOW_STATUS_LABELS[f.status] ?? f.status}
                          </span>
                          <span className={styles.entityName}>第 {f.planted_chapter} 章</span>
                          {f.resolved_chapter != null && (
                            <span className={styles.entityDesc}>→ 第 {f.resolved_chapter} 章回收</span>
                          )}
                        </span>
                        <span className={styles.entityDesc}>{f.description}</span>
                      </li>
                    )),
                )}
              </ul>
            )}
          </section>

          {world && (
            <>
              <section className={`panel ${styles.section}`}>
                <h2 className={styles.sectionTitle}>世界观</h2>
                <RealmOrder value={world.world_rules.realm_order} />
                <WorldRules world_rules={world.world_rules} />
              </section>

              <section className={`panel ${styles.section}`}>
                <h2 className={styles.sectionTitle}>硬约束</h2>
                {world.hard_constraints.length === 0 ? (
                  <div className="empty">暂无硬约束。</div>
                ) : (
                  <ul className={styles.constraintList}>
                    {world.hard_constraints.map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                )}
              </section>

              <section className={`panel ${styles.section}`}>
                <h2 className={styles.sectionTitle}>势力与地点</h2>
                <div className={styles.twoCol}>
                  <div>
                    <h3 className={styles.subTitle}>势力</h3>
                    {world.factions.length === 0 ? (
                      <div className="empty">暂无势力。</div>
                    ) : (
                      <ul className={styles.factionList}>
                        {world.factions.map((f) => (
                          <li key={f.name}>
                            <span className={styles.factionName}>{f.name}</span>
                            {f.stance && <span className={styles.factionStance}>{f.stance}</span>}
                            {f.resources.length > 0 && (
                              <span className={styles.factionResources}>资源：{f.resources.join('、')}</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div>
                    <h3 className={styles.subTitle}>地点</h3>
                    {world.locations.length === 0 ? (
                      <div className="empty">暂无地点。</div>
                    ) : (
                      <ul className={styles.locationList}>
                        {world.locations.map((l) => (
                          <li key={l.name}>{l.name}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              </section>
            </>
          )}
        </div>
      </main>
    </div>
  )
}
