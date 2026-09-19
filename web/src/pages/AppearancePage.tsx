// 账号级主题：官方主题 + 多套自定义。不绑具体书。
// 页面分两块：配色与背景图跟着某套预设走；字体与透明度是账号级的，官方三套一样吃。
// 两块都是实时预览：改动立刻作用到整页，只有点保存才写盘；没保存就退出会回到进来之前的样子。
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import { Link, useBlocker } from 'react-router-dom'
import { ProjectRail } from '../components/ProjectRail'
import { useAuth } from '../context/AuthContext'
import { useTheme } from '../context/ThemeContext'
import { api } from '../lib/api'
import { formatApiError } from '../lib/apiError'
import {
  applyTheme,
  applyWallpaper,
  clampChromeOpacity,
  clampPercent,
  clampWallpaperZoom,
  CUSTOM_TOKEN_FIELDS,
  DEFAULT_CUSTOM_TOKENS,
  DEFAULT_WALLPAPER,
  hexToRgba,
  isHexColor,
  newPresetId,
  nextPresetName,
  PRESET_LIMIT,
  THEMES,
  THEME_FONTS,
  THEME_FONT_SIZES,
  WALLPAPER_ZOOM_MAX,
  WALLPAPER_ZOOM_MIN,
  type CustomTokenKey,
  type CustomThemeTokens,
  type ThemeId,
  type ThemeStyle,
  type WallpaperConfig,
} from '../lib/theme'
import { clearWallpaper, readWallpaper, wallpaperError, writeWallpaper } from '../lib/themeImage'
import {
  OFFICIAL_THEME_TOKENS,
  PINNED_WORKSPACE_PREVIEW,
  WORKSPACE_PREVIEW_SIZE,
} from '../lib/themePreview'
import type { Project } from '../types'
import styles from './SettingsPage.module.css'

const preview = PINNED_WORKSPACE_PREVIEW

// 背景图预览框固定成 16:9，这样框里的铺法和整页一致，拖拽手感才对得上
const STAGE_ASPECT = 16 / 9
// 编辑器草稿。id 可能是还没落库的新预设；file / drop 是背景图草稿，保存时才写 IndexedDB。
// dirty 只标记「改过」，用来判断退出时要不要拦一下
type Editor = {
  id: string
  isNew: boolean
  name: string
  tokens: CustomThemeTokens
  wallpaper: WallpaperConfig
  file: File | null
  drop: boolean
  dirty: boolean
}

type OpenedTheme =
  | { kind: 'official'; id: Exclude<ThemeId, 'custom'> }
  | { kind: 'preset'; id: string }

// 收起编辑块和离开页面都会丢掉草稿，都要先问一句
type PendingExit = { kind: 'collapse' } | { kind: 'leave' }

function scale2(value: number): number {
  return Math.round(value * 100) / 100
}

/** 预览框里的铺法：和整页同一套算法，只是把 vw/vh 换成框自身的百分比 */
function stageStyle(image: string | null, config: WallpaperConfig): CSSProperties {
  const { aspect, fit, x, y, zoom } = config
  const scale = zoom / 100
  const coverWidth = scale2(Math.max(1, aspect / STAGE_ASPECT) * 100 * scale)
  const coverHeight = scale2(Math.max(STAGE_ASPECT / aspect, 1) * 100 * scale)
  return {
    backgroundImage: image ? `url("${image}")` : 'none',
    backgroundPosition: `${x}% ${y}%`,
    backgroundRepeat: fit === 'tile' ? 'repeat' : 'no-repeat',
    backgroundSize: fit === 'tile'
      ? `auto ${scale2(100 * scale)}%`
      : `${coverWidth}% ${coverHeight}%`,
  }
}

function previewStyle(
  tokens: CustomThemeTokens,
  style: ThemeStyle,
  image: string | null,
  wallpaper: WallpaperConfig,
): CSSProperties {
  const face = THEME_FONTS.find((item) => item.id === style.font) ?? THEME_FONTS[0]
  const size = THEME_FONT_SIZES.find((item) => item.id === style.fontSize) ?? THEME_FONT_SIZES[1]
  const alpha = clampChromeOpacity(style.chromeOpacity) / 100
  const mix = (value: number) => Math.round(Math.min(1, Math.max(0, value)) * 100) / 100
  return {
    '--preview-canvas': tokens.canvas,
    '--preview-ink': tokens.ink,
    '--preview-editor-ink': tokens.editorInk,
    '--preview-accent': tokens.accent,
    // 和整页一样：外壳和纸都跟着透明度走，纸的层级更高所以始终更实
    '--preview-chrome': hexToRgba(tokens.editor, mix(alpha * 0.86)),
    '--preview-editor': hexToRgba(tokens.editor, mix(alpha)),
    '--preview-accent-soft': hexToRgba(tokens.accent, mix(alpha * 0.34)),
    '--preview-font': face.css,
    '--preview-editor-size': size.editor,
    '--preview-image': image ? `url("${image}")` : 'none',
    '--preview-position': `${wallpaper.x}% ${wallpaper.y}%`,
  } as CSSProperties
}

function imageAspect(url: string, fallback: number): Promise<number> {
  return new Promise((resolve) => {
    const image = new Image()
    image.onload = () => resolve(image.naturalWidth / image.naturalHeight || fallback)
    image.onerror = () => resolve(fallback)
    image.src = url
  })
}

function WorkspacePreview(props: {
  tokens: CustomThemeTokens
  style: ThemeStyle
  image: string | null
  wallpaper: WallpaperConfig
  size: 'thumb' | 'large'
}) {
  const frame = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    const el = frame.current
    if (!el) return
    const fit = () => {
      const width = el.clientWidth
      el.style.setProperty('--preview-scale', width > 0 ? String(width / WORKSPACE_PREVIEW_SIZE.width) : '0.22')
    }
    fit()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(fit)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return (
    <div
      ref={frame}
      className={[
        styles.themePreview,
        props.size === 'large' ? styles.themePreviewLarge : '',
      ].filter(Boolean).join(' ')}
      style={previewStyle(props.tokens, props.style, props.image, props.wallpaper)}
      aria-hidden="true"
    >
      <div className={styles.themePreviewApp}>
        <div className={styles.themePreviewRail}>
          <div className={styles.themePreviewBrand}>{preview.brand}</div>
          <div className={styles.themePreviewRailList}>
            {preview.books.map((book) => (
              <div
                key={book}
                className={book === preview.book ? `${styles.themePreviewItem} ${styles.themePreviewItemActive}` : styles.themePreviewItem}
              >
                {book}
              </div>
            ))}
          </div>
          <div className={styles.themePreviewRailFoot}>
            {preview.bookLinks.map((link) => (
              <div key={link} className={styles.themePreviewItem}>{link}</div>
            ))}
            <div className={styles.themePreviewUser}>
              <span>{preview.user}</span>
              <span>登出</span>
            </div>
          </div>
        </div>
        <div className={styles.themePreviewPanel}>
          <div className={styles.themePreviewPanelHead}>
            <span>章节</span>
            <span>导出 .md</span>
            <span>删除本书</span>
          </div>
          {preview.chapters.map((chapter) => (
            <div
              key={chapter.seq}
              className={chapter.active ? `${styles.themePreviewChapter} ${styles.themePreviewItemActive}` : styles.themePreviewChapter}
            >
              <span>第 {chapter.seq} 章</span>
              <small>{chapter.words} 字</small>
              <small>{chapter.status}</small>
            </div>
          ))}
        </div>
        <div className={styles.themePreviewMain}>
          <div className={styles.themePreviewTabs}>
            <span>Plan<small>章节计划</small></span>
            <span className={styles.themePreviewTabActive}>正文<small>Write</small></span>
          </div>
          <div className={styles.themePreviewEditorHead}>
            <div>
              <div className={styles.themePreviewTitle}>{preview.title}</div>
              <small>{preview.badge} · {preview.version}</small>
            </div>
            <div className={styles.themePreviewTools}>
              <span>校正记忆</span>
              <span>删除本章</span>
              <span>历史版本</span>
            </div>
          </div>
          <div className={styles.themePreviewPaper}>
            <p className={styles.themePreviewBody}>{preview.body}</p>
          </div>
          <div className={styles.themePreviewEditorFoot}>
            <span>已保存</span>
            <span className={styles.themePreviewGhost}>保存</span>
          </div>
        </div>
        <div className={`${styles.themePreviewPanel} ${styles.themePreviewRight}`}>
          <div className={styles.themePreviewPanelHead}>
            <span>生成与重写</span>
            <small>下一章：第 2 章</small>
          </div>
          <div className={styles.themePreviewLabel}>写作指令（可留空）</div>
          <div className={`${styles.themePreviewField} ${styles.themePreviewInstruction}`}>
            如：节奏放慢，重点刻画战斗场面
          </div>
          <div className={styles.themePreviewAction}>写下一章（第 2 章）</div>
          <div className={styles.themePreviewLabel}>写作模式</div>
          <div className={styles.themePreviewModes}>
            <span className={styles.themePreviewItemActive}>自动</span>
            <span>手动确认 Plan</span>
          </div>
          <small>展示 Plan 后自动写作</small>
          <div className={styles.themePreviewGhost}>重写第 1 章</div>
          <div className={styles.themePreviewLabel}>批次生成</div>
          <div className={styles.themePreviewBatch}>
            <span className={styles.themePreviewField}>3</span>
            <small>从第 3 章起 ≈ ¥0.15</small>
          </div>
          <div className={styles.themePreviewGhost}>发起批次</div>
          <div className={styles.themePreviewPanelHead}>
            <span>章节流转</span>
            <small>第 1 章 · 已完成 · ¥0.0580</small>
          </div>
          <small>1 次生成 · 16 个阶段</small>
        </div>
      </div>
    </div>
  )
}

export default function AppearancePage() {
  const { logout } = useAuth()
  const {
    theme,
    presets,
    activePresetId,
    style,
    wallpaperUrl,
    setTheme,
    selectPreset,
    savePreset,
    deletePreset,
    saveStyle,
  } = useTheme()
  const [projects, setProjects] = useState<Project[]>([])
  const [presetImages, setPresetImages] = useState<Record<string, string>>({})
  const [banner, setBanner] = useState<string | null>(null)
  const [opened, setOpened] = useState<OpenedTheme | null>(null)

  const [editor, setEditor] = useState<Editor | null>(null)
  const [draftStyle, setDraftStyle] = useState<ThemeStyle>(style)
  const [draftUrl, setDraftUrl] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [pendingExit, setPendingExit] = useState<PendingExit | null>(null)
  const draftUrlRef = useRef<string | null>(null)
  const drag = useRef<{
    pointerId: number
    startX: number
    startY: number
    x: number
    y: number
    spanX: number
    spanY: number
  } | null>(null)

  const editing = editor !== null
  const activePreset = presets.find((item) => item.id === activePresetId) ?? null
  const savedEditingImage = editor && !editor.isNew ? presetImages[editor.id] ?? null : null
  const draftImage = editor
    ? editor.file
      ? draftUrl
      : editor.drop
        ? null
        : savedEditingImage
    : null
  const styleDirty = draftStyle.chromeOpacity !== style.chromeOpacity
    || draftStyle.font !== style.font
    || draftStyle.fontSize !== style.fontSize
  const editorDirty = editor?.dirty ?? false
  const unsaved = editorDirty || styleDirty
  const previewing = editing || styleDirty

  // 退出预览要回到的那套：即便中途点了删除，这里也始终是当前已保存的状态
  const restore = useCallback(() => {
    applyTheme(theme, activePreset?.tokens, style)
    applyWallpaper(theme === 'custom' ? wallpaperUrl : null, activePreset?.wallpaper)
  }, [theme, style, wallpaperUrl, activePreset])
  const restoreRef = useRef(restore)
  restoreRef.current = restore

  // 走路由离开页面前拦一下，别让没保存的改动悄悄没了。用 ref 读最新的脏状态，好让回调保持稳定
  const unsavedRef = useRef(false)
  unsavedRef.current = unsaved
  const blocker = useBlocker(useCallback(() => unsavedRef.current, []))
  const askingExit = pendingExit !== null || blocker.state === 'blocked'

  const cancelExit = useCallback(() => {
    setPendingExit(null)
    if (blocker.state === 'blocked') blocker.reset()
  }, [blocker])

  function confirmExit() {
    const pending = pendingExit
    setPendingExit(null)
    if (blocker.state === 'blocked') {
      blocker.proceed()
      return
    }
    if (pending?.kind === 'collapse') closeEditor()
  }

  useEffect(() => {
    api.listProjects().then(setProjects).catch((err) => {
      setBanner(formatApiError(err, '作品列表加载失败'))
    })
  }, [])

  useEffect(() => {
    let cancelled = false
    const created: string[] = []
    void Promise.all(presets.map(async (item) => {
      const blob = await readWallpaper(item.id)
      if (!blob) return [item.id, ''] as const
      const url = URL.createObjectURL(blob)
      created.push(url)
      return [item.id, url] as const
    })).then((entries) => {
      if (cancelled) {
        created.forEach((url) => URL.revokeObjectURL(url))
        return
      }
      setPresetImages(Object.fromEntries(entries.filter(([, url]) => url)))
    })
    return () => {
      cancelled = true
      created.forEach((url) => URL.revokeObjectURL(url))
    }
  }, [presets])

  useEffect(() => () => {
    if (draftUrlRef.current) URL.revokeObjectURL(draftUrlRef.current)
  }, [])

  useEffect(() => {
    if (!opened) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpened(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [opened])

  useEffect(() => {
    if (!askingExit) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') cancelExit()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [askingExit, cancelExit])

  // 实时预览：配色草稿和字体/透明度草稿一改就作用到整页（官方主题也吃字体和透明度）
  useEffect(() => {
    if (!previewing) return
    if (editor) {
      applyTheme('custom', editor.tokens, draftStyle)
      applyWallpaper(draftImage, editor.wallpaper)
      return
    }
    applyTheme(theme, activePreset?.tokens, draftStyle)
  }, [previewing, editor, draftImage, draftStyle, theme, activePreset])

  // 收起 / 保存 / 离开页面：把整页还原成盘上那套
  useEffect(() => {
    if (!previewing) return
    return () => restoreRef.current()
  }, [previewing])

  function replaceDraftUrl(url: string | null) {
    if (draftUrlRef.current) URL.revokeObjectURL(draftUrlRef.current)
    draftUrlRef.current = url
    setDraftUrl(url)
  }

  function openEditor(next: Editor) {
    replaceDraftUrl(null)
    setBanner(null)
    setConfirmDelete(false)
    setEditor(next)
  }

  function closeEditor() {
    replaceDraftUrl(null)
    setConfirmDelete(false)
    setEditor(null)
  }

  // 收起会丢掉草稿，改过就先问一句
  function onCollapse() {
    if (editorDirty) {
      setPendingExit({ kind: 'collapse' })
      return
    }
    closeEditor()
  }

  function onCreate() {
    if (presets.length >= PRESET_LIMIT) {
      setBanner(`最多 ${PRESET_LIMIT} 套自定义`)
      return
    }
    openEditor({
      id: newPresetId(),
      isNew: true,
      name: nextPresetName(presets),
      tokens: { ...DEFAULT_CUSTOM_TOKENS },
      wallpaper: { ...DEFAULT_WALLPAPER },
      file: null,
      drop: false,
      dirty: false,
    })
  }

  function editPreset(id: string) {
    const preset = presets.find((item) => item.id === id)
    if (!preset) return
    openEditor({
      id,
      isNew: false,
      name: preset.name,
      tokens: { ...preset.tokens },
      wallpaper: { ...preset.wallpaper },
      file: null,
      drop: false,
      dirty: false,
    })
  }

  function patch(next: Partial<Editor>) {
    setEditor((current) => (current ? { ...current, ...next, dirty: true } : current))
  }

  function patchTokens(key: CustomTokenKey, value: string) {
    setEditor((current) => (current
      ? { ...current, tokens: { ...current.tokens, [key]: value }, dirty: true }
      : current))
  }

  function patchWallpaper(next: Partial<WallpaperConfig>) {
    setEditor((current) => (current
      ? { ...current, wallpaper: { ...current.wallpaper, ...next }, dirty: true }
      : current))
  }

  function onPickWallpaper(file: File | undefined) {
    if (!file || !editor) return
    const error = wallpaperError(file)
    if (error) {
      setBanner(error)
      return
    }
    setBanner(null)
    replaceDraftUrl(URL.createObjectURL(file))
    const aspect = editor.wallpaper.aspect
    patch({ file, drop: false })
    void imageAspect(draftUrlRef.current ?? '', aspect).then((next) => {
      setEditor((current) => (current?.file === file
        ? { ...current, wallpaper: { ...current.wallpaper, aspect: next } }
        : current))
    })
  }

  function onDropWallpaper() {
    replaceDraftUrl(null)
    patch({ file: null, drop: true })
  }

  function onSave() {
    if (!editor) return
    const tokens = { ...editor.tokens }
    for (const field of CUSTOM_TOKEN_FIELDS) {
      if (!isHexColor(tokens[field.key])) {
        setBanner(`${field.label}需要是 #RGB 或 #RRGGBB`)
        return
      }
      tokens[field.key] = tokens[field.key].trim()
    }
    if (!editor.name.trim()) {
      setBanner('先给这套起个名字')
      return
    }
    setBanner(null)
    const saved = {
      id: editor.id,
      name: editor.name.trim(),
      tokens,
      wallpaper: editor.wallpaper,
    }
    // 背景图先落库再保存主题：保存会重新读盘，读到的是刚写进去的新图
    const commit = () => {
      savePreset(saved.id, saved.name, saved.tokens, saved.wallpaper)
      // file / drop 留着不清：编辑期间草稿图才是整页背景的准头，清掉会闪一下空白
      setEditor((current) => (current
        ? { ...current, isNew: false, name: saved.name, tokens: saved.tokens, dirty: false }
        : current))
    }
    if (editor.file) void writeWallpaper(editor.id, editor.file).then(commit)
    else if (editor.drop) void clearWallpaper(editor.id).then(commit)
    else commit()
  }

  function onSaveStyle() {
    saveStyle(draftStyle)
  }

  function onDelete() {    if (!editor) return
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    if (!editor.isNew) deletePreset(editor.id)
    closeEditor()
  }

  function onWallpaperPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!editor || !draftImage) return
    const box = event.currentTarget
    const boxWidth = box.clientWidth
    const boxHeight = box.clientHeight
    const { aspect, x, y, zoom } = editor.wallpaper
    const scale = zoom / 100
    drag.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      x,
      y,
      // background-position 是百分比，拖动一格能移动多少取决于图片比框大出多少
      spanX: Math.max(boxWidth, (boxWidth * aspect) / STAGE_ASPECT) * scale - boxWidth,
      spanY: Math.max((boxHeight * STAGE_ASPECT) / aspect, boxHeight) * scale - boxHeight,
    }
    box.setPointerCapture(event.pointerId)
  }

  function onWallpaperPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const state = drag.current
    if (!state || state.pointerId !== event.pointerId) return
    const x = state.spanX > 1
      ? clampPercent(state.x - ((event.clientX - state.startX) / state.spanX) * 100, state.x)
      : state.x
    const y = state.spanY > 1
      ? clampPercent(state.y - ((event.clientY - state.startY) / state.spanY) * 100, state.y)
      : state.y
    patchWallpaper({ x, y })
  }

  function onWallpaperPointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    const state = drag.current
    if (!state || state.pointerId !== event.pointerId) return
    drag.current = null
    event.currentTarget.releasePointerCapture(event.pointerId)
  }

  const openedView = opened && (
    opened.kind === 'official'
      ? {
        label: THEMES.find((item) => item.id === opened.id)?.label ?? opened.id,
        tokens: OFFICIAL_THEME_TOKENS[opened.id],
        image: null as string | null,
        wallpaper: DEFAULT_WALLPAPER,
        active: opened.id === theme,
        apply: () => setTheme(opened.id),
      }
      : (() => {
        const item = presets.find((preset) => preset.id === opened.id)
        if (!item) return null
        return {
          label: item.name,
          tokens: item.tokens,
          image: presetImages[item.id] ?? null,
          wallpaper: item.wallpaper,
          active: theme === 'custom' && item.id === activePresetId,
          apply: () => selectPreset(item.id),
        }
      })()
  )

  function applyOpened() {
    openedView?.apply()
    setOpened(null)
  }

  return (
    <div className={styles.wrap}>
      <ProjectRail projects={projects} onLogout={logout} />
      <main className={styles.main}>
        <div className={styles.inner}>
          <header className={styles.header}>
            <div>
              <h1>主题</h1>
              <div className={styles.crumb}>
                <Link to="/projects">返回作品库</Link>
              </div>
            </div>
          </header>

          {banner && <div className="banner banner-error">{banner}</div>}

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>已有配置</h2>
            <div className={styles.themeGrid} role="group" aria-label="主题">
              {THEMES.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={item.id === theme ? `${styles.themeCard} ${styles.themeCardActive}` : styles.themeCard}
                  aria-label={item.label}
                  aria-pressed={item.id === theme}
                  onClick={() => setOpened({ kind: 'official', id: item.id })}
                >
                  <WorkspacePreview
                    size="thumb"
                    tokens={OFFICIAL_THEME_TOKENS[item.id]}
                    style={draftStyle}
                    image={null}
                    wallpaper={DEFAULT_WALLPAPER}
                  />
                  <span className={styles.themeCardMeta}>
                    <span className={styles.themeCardTitle}>{item.label}</span>
                  </span>
                </button>
              ))}
              {presets.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={theme === 'custom' && item.id === activePresetId ? `${styles.themeCard} ${styles.themeCardActive}` : styles.themeCard}
                  aria-label={item.name}
                  aria-pressed={theme === 'custom' && item.id === activePresetId}
                  onClick={() => setOpened({ kind: 'preset', id: item.id })}
                >
                  <WorkspacePreview
                    size="thumb"
                    tokens={item.tokens}
                    style={draftStyle}
                    image={presetImages[item.id] ?? null}
                    wallpaper={item.wallpaper}
                  />
                  <span className={styles.themeCardMeta}>
                    <span className={styles.themeCardTitle}>{item.name}</span>
                  </span>
                </button>
              ))}
            </div>
            <div>
              <button type="button" className="btn btn-secondary" onClick={onCreate}>
                新建自定义
              </button>
            </div>
          </section>

          <section className={`panel ${styles.section}`}>
            <h2 className={styles.sectionTitle}>字体与透明度</h2>
            <div className={styles.themeEditor}>
              <div className={styles.themeEditorCol}>
                <div className={styles.themeField}>
                  <span className={styles.fieldLabel}>正文字体</span>
                  <p className={styles.hint}>界面使用系统清晰字体；这里仅调整正文。普通显示器建议黑体，也可调大字号。</p>
                  <div className={styles.themeChoiceRow}>
                    {THEME_FONTS.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className={draftStyle.font === item.id ? `${styles.themeChoice} ${styles.themeChoiceActive}` : styles.themeChoice}
                        aria-label={item.label}
                        aria-pressed={draftStyle.font === item.id}
                        style={{ fontFamily: item.css }}
                        onClick={() => setDraftStyle((current) => ({ ...current, font: item.id }))}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>

                <div className={styles.themeField}>
                  <span className={styles.fieldLabel}>字号</span>
                  <div className={styles.themeChoiceRow}>
                    {THEME_FONT_SIZES.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className={draftStyle.fontSize === item.id ? `${styles.themeChoice} ${styles.themeChoiceActive}` : styles.themeChoice}
                        aria-label={item.label}
                        aria-pressed={draftStyle.fontSize === item.id}
                        onClick={() => setDraftStyle((current) => ({ ...current, fontSize: item.id }))}
                      >
                        <span className={styles.themeChoiceSample} style={{ fontSize: item.editor }}>永</span>
                        <span className={styles.themeChoiceLabel}>{item.label}</span>
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className={styles.themeEditorCol}>
                <label className={styles.themeField}>
                  <span className={styles.fieldLabel}>界面透明度</span>
                  <span className={styles.themeSwatchRow}>
                    <input
                      type="range"
                      min={20}
                      max={100}
                      aria-label="界面透明度"
                      value={draftStyle.chromeOpacity}
                      onChange={(event) => setDraftStyle((current) => ({
                        ...current,
                        chromeOpacity: clampChromeOpacity(Number(event.target.value)),
                      }))}
                    />
                    <span className={styles.themeSliderValue}>{draftStyle.chromeOpacity}%</span>
                  </span>
                </label>

                <div className={styles.themeActions}>
                  <button type="button" className="btn btn-primary" onClick={onSaveStyle} disabled={!styleDirty}>
                    保存
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => setDraftStyle(style)}
                    disabled={!styleDirty}
                  >
                    还原
                  </button>
                </div>
              </div>
            </div>
          </section>

          {editor && (
            <section className={`panel ${styles.section}`}>
              <div className={styles.themeEditorHead}>
                <h2 className={styles.sectionTitle}>{editor.isNew ? '新建自定义' : `配色与背景图 · ${editor.name}`}</h2>
                <button type="button" className="btn btn-quiet" onClick={onCollapse}>
                  收起
                </button>
              </div>

              <div className={styles.themeEditor}>
                <div className={styles.themeEditorCol}>
                  <label className={styles.themeField}>
                    <span className={styles.fieldLabel}>名称</span>
                    <input
                      className="input"
                      aria-label="名称"
                      value={editor.name}
                      onChange={(event) => patch({ name: event.target.value })}
                    />
                  </label>

                  <div className={styles.themeFields}>
                    {CUSTOM_TOKEN_FIELDS.map((field) => (
                      <label key={field.key} className={styles.themeField}>
                        <span className={styles.fieldLabel}>{field.label}</span>
                        <span className={styles.themeSwatchRow}>
                          <input
                            type="color"
                            aria-label={`${field.label}色板`}
                            value={isHexColor(editor.tokens[field.key]) && editor.tokens[field.key].length !== 4
                              ? editor.tokens[field.key]
                              : '#000000'}
                            onChange={(event) => patchTokens(field.key, event.target.value)}
                          />
                          <input
                            className="input"
                            aria-label={field.label}
                            value={editor.tokens[field.key]}
                            onChange={(event) => patchTokens(field.key, event.target.value)}
                          />
                        </span>
                      </label>
                    ))}
                  </div>

                </div>

                <div className={styles.themeEditorCol}>
                  <div className={styles.themeField}>
                    <span className={styles.fieldLabel}>背景图</span>
                    <div
                      className={styles.themeWallpaperStage}
                      style={stageStyle(draftImage, editor.wallpaper)}
                      role="presentation"
                      onPointerDown={onWallpaperPointerDown}
                      onPointerMove={onWallpaperPointerMove}
                      onPointerUp={onWallpaperPointerUp}
                      onPointerCancel={onWallpaperPointerUp}
                    />
                    <div className={styles.themeWallpaperActions}>
                      <label className={`btn btn-secondary ${styles.themeWallpaperPick}`}>
                        {draftImage ? '换一张' : '选择图片'}
                        <input
                          className={styles.themeWallpaperInput}
                          type="file"
                          accept="image/jpeg,image/png,image/webp,image/gif"
                          aria-label="上传背景图"
                          onChange={(event) => {
                            onPickWallpaper(event.target.files?.[0])
                            event.target.value = ''
                          }}
                        />
                      </label>
                      {draftImage && (
                        <button type="button" className="btn btn-quiet" onClick={onDropWallpaper}>
                          去掉
                        </button>
                      )}
                    </div>
                  </div>

                  <div className={styles.themeWallpaperRow}>
                    <span className={styles.fieldLabel}>铺法</span>
                    <div className={styles.themeChoiceRow}>
                      {([['cover', '铺满'], ['tile', '平铺']] as const).map(([fit, label]) => (
                        <button
                          key={fit}
                          type="button"
                          className={`${styles.themeChoice} ${styles.themeChoiceTight} ${editor.wallpaper.fit === fit ? styles.themeChoiceActive : ''}`}
                          aria-pressed={editor.wallpaper.fit === fit}
                          onClick={() => patchWallpaper({ fit })}
                        >
                          <span className={styles.themeChoiceLabel}>{label}</span>
                        </button>
                      ))}
                    </div>
                    <span className={styles.fieldLabel}>缩放</span>
                    <input
                      className={styles.themeWallpaperZoom}
                      type="range"
                      min={WALLPAPER_ZOOM_MIN}
                      max={WALLPAPER_ZOOM_MAX}
                      aria-label="图片缩放"
                      value={editor.wallpaper.zoom}
                      onChange={(event) => patchWallpaper({ zoom: clampWallpaperZoom(Number(event.target.value)) })}
                    />
                    <span className={styles.themeSliderValue}>{editor.wallpaper.zoom}%</span>
                  </div>
                </div>
              </div>

              <div className={styles.themeActions}>
                <button type="button" className="btn btn-primary" onClick={onSave}>
                  保存这套
                </button>
                <button
                  type="button"
                  className={`btn ${confirmDelete ? styles.themeDeleteArmed : styles.themeDelete}`}
                  onClick={onDelete}
                >
                  {confirmDelete ? '确认删除' : '删除这套'}
                </button>
                {confirmDelete && (
                  <button type="button" className="btn btn-quiet" onClick={() => setConfirmDelete(false)}>
                    取消
                  </button>
                )}
              </div>
            </section>
          )}
        </div>
      </main>

      {openedView && (
        <div
          className={styles.themeDialogBackdrop}
          onClick={(event) => {
            if (event.target === event.currentTarget) setOpened(null)
          }}
        >
          <div
            className={styles.themeDialog}
            role="dialog"
            aria-modal="true"
            aria-labelledby="theme-preview-title"
          >
            <div className={styles.themeDialogHead}>
              <div>
                <h2 id="theme-preview-title">{openedView.label}</h2>
                {openedView.active && <span className="badge badge-success">使用中</span>}
              </div>
              <button type="button" className="btn btn-quiet" onClick={() => setOpened(null)}>
                关闭
              </button>
            </div>
            <WorkspacePreview
              size="large"
              tokens={openedView.tokens}
              style={draftStyle}
              image={openedView.image}
              wallpaper={openedView.wallpaper}
            />
            <div className={styles.themeDialogActions}>
              <button type="button" className="btn btn-secondary" onClick={() => setOpened(null)}>
                先不应用
              </button>
              {opened?.kind === 'preset' && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    const id = opened.id
                    setOpened(null)
                    editPreset(id)
                  }}
                >
                  编辑这套
                </button>
              )}
              <button
                type="button"
                className="btn btn-primary"
                onClick={applyOpened}
                disabled={openedView.active}
              >
                应用这套
              </button>
            </div>
          </div>
        </div>
      )}

      {askingExit && (
        <div
          className={styles.themeDialogBackdrop}
          onClick={(event) => {
            if (event.target === event.currentTarget) cancelExit()
          }}
        >
          <div
            className={`${styles.themeDialog} ${styles.themeConfirm}`}
            role="dialog"
            aria-modal="true"
            aria-labelledby="theme-exit-title"
          >
            <h2 id="theme-exit-title" className={styles.themeConfirmTitle}>还没保存</h2>
            <p className={styles.themeConfirmText}>直接退出的话，刚才改的东西不会保留。</p>
            <div className={styles.themeDialogActions}>
              <button type="button" className="btn btn-secondary" onClick={cancelExit}>
                取消
              </button>
              <button type="button" className="btn btn-primary" onClick={confirmExit}>
                确认
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
