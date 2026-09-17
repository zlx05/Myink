// 账号级外观：官方三套写在 tokens.css；自定义是多套预设，写到 html 的 CSS 变量。
// 分两块：配色/背景图是每套预设自己的（见 CustomPreset），字体与透明度是账号级的，
// 官方三套一样吃（见 ThemeStyle）。

export const THEME_STORAGE_KEY = 'aiink.theme'
export const CUSTOM_THEME_STORAGE_KEY = 'aiink.theme.custom'
export const PRESET_STORAGE_KEY = 'aiink.theme.presets'
export const STYLE_STORAGE_KEY = 'aiink.theme.style'
export const PRESET_LIMIT = 8

export const THEME_IDS = ['paper', 'night', 'contrast', 'custom'] as const
export type ThemeId = (typeof THEME_IDS)[number]

export const THEMES: ReadonlyArray<{ id: Exclude<ThemeId, 'custom'>; label: string }> = [
  { id: 'paper', label: '纸感' },
  { id: 'night', label: '夜间' },
  { id: 'contrast', label: '高对比' },
]

export const CUSTOM_TOKEN_FIELDS = [
  { key: 'canvas', css: '--canvas', label: '整页底' },
  { key: 'ink', css: '--ink', label: '主文字' },
  { key: 'editor', css: '--editor', label: '编辑纸' },
  { key: 'editorInk', css: '--editor-ink', label: '纸上的字' },
  { key: 'accent', css: '--accent', label: '强调色' },
] as const

export type CustomTokenKey = (typeof CUSTOM_TOKEN_FIELDS)[number]['key']
export type ThemeFont = 'song' | 'kai' | 'hei'
export type ThemeFontSize = 's' | 'm' | 'l' | 'xl'

export const THEME_FONTS: ReadonlyArray<{ id: ThemeFont; label: string; css: string }> = [
  // 宋体这条与 tokens.css 里 --font-editor 的原值一致：默认档位下不改变任何主题的排版
  { id: 'song', label: '宋体', css: "Georgia, 'Songti SC', 'SimSun', 'Noto Serif SC', serif" },
  { id: 'kai', label: '楷体', css: "'Kaiti SC', 'STKaiti', 'KaiTi', 'Noto Serif SC', serif" },
  { id: 'hei', label: '黑体', css: "'Heiti SC', 'STHeiti', 'PingFang SC', 'SimHei', 'Microsoft YaHei', sans-serif" },
]

export const THEME_FONT_SIZES: ReadonlyArray<{
  id: ThemeFontSize
  label: string
  editor: string
  body: string
  small: string
  heading: string
}> = [
  { id: 's', label: '小', editor: '15px', body: '13px', small: '11px', heading: '16px' },
  { id: 'm', label: '中', editor: '17px', body: '14px', small: '12px', heading: '18px' },
  { id: 'l', label: '大', editor: '19px', body: '15px', small: '13px', heading: '20px' },
  { id: 'xl', label: '特大', editor: '21px', body: '16px', small: '14px', heading: '22px' },
]

// 每套预设只管配色：底色/文字/纸/强调色
export type CustomThemeTokens = Record<CustomTokenKey, string>

// 字体与透明度不属于任何一套配色，它们是账号级的，官方三套一样跟着变
export type ThemeStyle = {
  chromeOpacity: number
  font: ThemeFont
  fontSize: ThemeFontSize
}

export type WallpaperFit = 'cover' | 'tile'

export type WallpaperConfig = {
  fit: WallpaperFit
  /** 铺满时在 cover 的基础上再放大，100 表示刚好铺满 */
  zoom: number
  /** background-position 的百分比，预览框里拖拽改的就是这两个 */
  x: number
  y: number
  /** 图片宽高比，用来算 cover 需要的尺寸 */
  aspect: number
}

export const DEFAULT_WALLPAPER: WallpaperConfig = {
  fit: 'cover',
  zoom: 100,
  x: 50,
  y: 50,
  aspect: 16 / 9,
}

export const WALLPAPER_ZOOM_MIN = 100
export const WALLPAPER_ZOOM_MAX = 300

export type CustomPreset = {
  id: string
  name: string
  tokens: CustomThemeTokens
  wallpaper: WallpaperConfig
}

export type PresetStore = {
  activeId: string | null
  items: CustomPreset[]
}

export const DEFAULT_CUSTOM_TOKENS: CustomThemeTokens = {
  canvas: '#f6f3e9',
  ink: '#3f3a36',
  editor: '#fffdf7',
  editorInk: '#514a43',
  accent: '#9fd8ad',
}

// 100 表示原样不透明；往下拉是把每一层等比调透，见 applyTheme
export const DEFAULT_THEME_STYLE: ThemeStyle = {
  chromeOpacity: 100,
  font: 'song',
  fontSize: 'm',
}

// 外壳各层在设计上的基准不透明度。100% 时就是设计原样，往下拉每层乘同一个系数，
// 于是正文纸和外壳一起变淡，但正文始终比外壳实，靠层次而不是靠不透明区分。
const SHELL_LAYERS: ReadonlyArray<readonly [string, number]> = [
  ['--editor', 1],
  ['--surface-1', 0.86],
  ['--surface-2', 0.92],
  ['--surface-3', 0.96],
  ['--rail-bg', 0.82],
  ['--input-bg', 0.86],
  ['--rail-item-hover', 0.9],
  ['--quiet-hover', 0.92],
]

const INK_LAYERS: ReadonlyArray<readonly [string, number]> = [
  ['--line', 0.2],
  ['--line-strong', 0.32],
]

const ACCENT_LAYERS: ReadonlyArray<readonly [string, number]> = [
  ['--accent-soft', 0.6],
  ['--badge-success-bg', 0.45],
  ['--badge-success-border', 0.85],
  ['--lavender-soft', 0.35],
  ['--peach-soft', 0.35],
]

// 官方三套的外壳色板，与 tokens.css 一一对应。透明度滑杆就是把这些底色按比例稀释；
// 100% 时整块跳过，官方主题完全走样式表，跟以前一模一样。
const THEME_SHELLS: Record<Exclude<ThemeId, 'custom'>, ReadonlyArray<readonly [string, string]>> = {
  paper: [
    ['--editor', '#fffdf7'],
    ['--surface-1', 'rgba(255, 255, 252, 0.86)'],
    ['--surface-2', '#f4f0e8'],
    ['--surface-3', '#ece8de'],
    ['--rail-bg', 'rgba(255, 255, 252, 0.77)'],
    ['--input-bg', 'rgba(255, 255, 252, 0.78)'],
    ['--rail-item-hover', '#f3efe6'],
    ['--quiet-hover', '#f0ece4'],
    ['--line', '#e8e1d5'],
    ['--line-strong', '#d8cfc0'],
    ['--accent-soft', '#e2f3df'],
    ['--badge-success-bg', '#eef9ee'],
    ['--badge-success-border', '#add3b9'],
    ['--lavender-soft', '#ebe5f4'],
    ['--peach-soft', '#fbe3de'],
  ],
  night: [
    ['--editor', '#181818'],
    ['--surface-1', '#1c1c1c'],
    ['--surface-2', '#242424'],
    ['--surface-3', '#2c2c2c'],
    ['--rail-bg', '#161616'],
    ['--input-bg', '#1c1c1c'],
    ['--rail-item-hover', '#2a2a2a'],
    ['--quiet-hover', '#2a2a2a'],
    ['--line', '#333333'],
    ['--line-strong', '#454545'],
    ['--accent-soft', '#1e2a22'],
    ['--badge-success-bg', '#1e2a22'],
    ['--badge-success-border', '#5f8f6e'],
    ['--lavender-soft', '#22222a'],
    ['--peach-soft', '#2a2220'],
  ],
  contrast: [
    ['--editor', '#ffffff'],
    ['--surface-1', '#ffffff'],
    ['--surface-2', '#f4f4f4'],
    ['--surface-3', '#ebebeb'],
    ['--rail-bg', '#f7f7f7'],
    ['--input-bg', '#ffffff'],
    ['--rail-item-hover', '#ececec'],
    ['--quiet-hover', '#ececec'],
    ['--line', '#c8c8c8'],
    ['--line-strong', '#8a8a8a'],
    ['--accent-soft', '#d9f0e0'],
    ['--badge-success-bg', '#e3f5e8'],
    ['--badge-success-border', '#1f7a3a'],
    ['--lavender-soft', '#e6e0f0'],
    ['--peach-soft', '#f8d7d0'],
  ],
}

const CUSTOM_INLINE_VARS = [
  ...CUSTOM_TOKEN_FIELDS.map((field) => field.css),
  '--accent-hover',
  '--accent-pressed',
  '--accent-soft',
  '--focus',
  '--btn-primary-border',
  '--btn-primary-ink',
  '--heading',
  '--link-hover',
  '--surface-1',
  '--surface-2',
  '--surface-3',
  '--rail-bg',
  '--rail-item-hover',
  '--input-bg',
  '--quiet-hover',
  '--line',
  '--line-strong',
  '--badge-success-bg',
  '--badge-success-border',
  '--lavender-soft',
  '--peach-soft',
  '--font-ui',
  '--font-editor',
  '--text-editor',
  '--text-body',
  '--text-small',
  '--text-heading',
]

export function isThemeId(value: unknown): value is ThemeId {
  return THEME_IDS.includes(value as ThemeId)
}

export function isHexColor(value: string): boolean {
  return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(value.trim())
}

export function clampChromeOpacity(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_THEME_STYLE.chromeOpacity
  return Math.min(100, Math.max(20, Math.round(value)))
}

export function parseThemeStyle(raw: unknown): ThemeStyle {
  const next = { ...DEFAULT_THEME_STYLE }
  if (!raw || typeof raw !== 'object') return next
  const parsed = raw as Partial<ThemeStyle>
  if (typeof parsed.chromeOpacity === 'number') next.chromeOpacity = clampChromeOpacity(parsed.chromeOpacity)
  if (parsed.font === 'song' || parsed.font === 'kai' || parsed.font === 'hei') next.font = parsed.font
  if (parsed.fontSize === 's' || parsed.fontSize === 'm' || parsed.fontSize === 'l' || parsed.fontSize === 'xl') {
    next.fontSize = parsed.fontSize
  }
  return next
}

export function readThemeStyle(): ThemeStyle {
  try {
    const raw = localStorage.getItem(STYLE_STORAGE_KEY)
    return raw ? parseThemeStyle(JSON.parse(raw) as unknown) : { ...DEFAULT_THEME_STYLE }
  } catch {
    return { ...DEFAULT_THEME_STYLE }
  }
}

export function writeThemeStyle(style: ThemeStyle): void {
  localStorage.setItem(STYLE_STORAGE_KEY, JSON.stringify(style))
}

export function clampWallpaperZoom(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_WALLPAPER.zoom
  return Math.min(WALLPAPER_ZOOM_MAX, Math.max(WALLPAPER_ZOOM_MIN, Math.round(value)))
}

export function clampPercent(value: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback
  return Math.min(100, Math.max(0, Math.round(value)))
}

export function parseWallpaper(raw: unknown): WallpaperConfig {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_WALLPAPER }
  const item = raw as Partial<WallpaperConfig>
  const aspect = typeof item.aspect === 'number' && Number.isFinite(item.aspect) && item.aspect > 0
    ? item.aspect
    : DEFAULT_WALLPAPER.aspect
  return {
    fit: item.fit === 'tile' ? 'tile' : 'cover',
    zoom: clampWallpaperZoom(item.zoom ?? DEFAULT_WALLPAPER.zoom),
    x: clampPercent(item.x ?? DEFAULT_WALLPAPER.x, DEFAULT_WALLPAPER.x),
    y: clampPercent(item.y ?? DEFAULT_WALLPAPER.y, DEFAULT_WALLPAPER.y),
    aspect,
  }
}

function round4(value: number): number {
  return Math.round(value * 10000) / 10000
}

export function hexToRgba(hex: string, alpha: number): string {
  const raw = hex.trim().replace('#', '')
  const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw
  const r = Number.parseInt(full.slice(0, 2), 16)
  const g = Number.parseInt(full.slice(2, 4), 16)
  const b = Number.parseInt(full.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

export function shadeHex(hex: string, factor: number): string {
  const raw = hex.trim().replace('#', '')
  const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw
  const scaled = [0, 2, 4].map((start) => {
    const channel = Number.parseInt(full.slice(start, start + 2), 16)
    return Math.max(0, Math.min(255, Math.round(channel * factor)))
      .toString(16)
      .padStart(2, '0')
  })
  return `#${scaled.join('')}`
}

export function parseCustomTokens(raw: unknown): CustomThemeTokens {
  const next = { ...DEFAULT_CUSTOM_TOKENS }
  if (!raw || typeof raw !== 'object') return next
  const parsed = raw as Partial<CustomThemeTokens>
  for (const field of CUSTOM_TOKEN_FIELDS) {
    const value = parsed[field.key]
    if (typeof value === 'string' && isHexColor(value)) next[field.key] = value.trim()
  }
  return next
}

export function newPresetId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `preset-${Date.now()}`
}

export function nextPresetName(items: CustomPreset[]): string {
  const used = new Set(items.map((item) => item.name))
  for (let n = 1; n < 100; n += 1) {
    const name = `自定义 ${n}`
    if (!used.has(name)) return name
  }
  return `自定义 ${items.length + 1}`
}

function displayPresetName(name: string, used: Set<string>): string {
  const match = name.match(/^预设 (\d+)$/)
  if (!match) return name
  const preferred = `自定义 ${match[1]}`
  if (!used.has(preferred)) return preferred
  for (let n = 1; n < 100; n += 1) {
    const candidate = `自定义 ${n}`
    if (!used.has(candidate)) return candidate
  }
  return preferred
}

function parsePreset(raw: unknown): CustomPreset | null {
  if (!raw || typeof raw !== 'object') return null
  const item = raw as Partial<CustomPreset>
  if (typeof item.id !== 'string' || !item.id) return null
  const name = typeof item.name === 'string' && item.name.trim() ? item.name.trim() : '未命名'
  return {
    id: item.id,
    name,
    tokens: parseCustomTokens(item.tokens),
    wallpaper: parseWallpaper(item.wallpaper),
  }
}

export function readTheme(): ThemeId {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY)
    return isThemeId(raw) ? raw : 'paper'
  } catch {
    return 'paper'
  }
}

export function writeTheme(id: ThemeId): void {
  localStorage.setItem(THEME_STORAGE_KEY, id)
}

function migrateLegacyPreset(): PresetStore {
  try {
    const raw = localStorage.getItem(CUSTOM_THEME_STORAGE_KEY)
    if (!raw) return { activeId: null, items: [] }
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object' || 'items' in parsed) return { activeId: null, items: [] }
    const preset = {
      id: 'legacy',
      name: '自定义 1',
      tokens: parseCustomTokens(parsed),
      wallpaper: { ...DEFAULT_WALLPAPER },
    }
    return { activeId: 'legacy', items: [preset] }
  } catch {
    return { activeId: null, items: [] }
  }
}

export function readPresetStore(): PresetStore {
  try {
    const raw = localStorage.getItem(PRESET_STORAGE_KEY)
    if (!raw) return migrateLegacyPreset()
    const parsed = JSON.parse(raw) as Partial<PresetStore>
    const items = Array.isArray(parsed.items)
      ? parsed.items.map(parsePreset).filter((item): item is CustomPreset => item !== null)
      : []
    const taken = new Set(items.map((item) => item.name).filter((name) => !/^预设 \d+$/.test(name)))
    const renamed = items.map((item) => {
      const name = displayPresetName(item.name, taken)
      taken.add(name)
      return name === item.name ? item : { ...item, name }
    })
    const activeId = typeof parsed.activeId === 'string' && renamed.some((item) => item.id === parsed.activeId)
      ? parsed.activeId
      : renamed[0]?.id ?? null
    return { activeId, items: renamed }
  } catch {
    return { activeId: null, items: [] }
  }
}

export function writePresetStore(store: PresetStore): void {
  localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(store))
}

export function readActivePreset(): CustomPreset | null {
  const store = readPresetStore()
  return store.items.find((item) => item.id === store.activeId) ?? store.items[0] ?? null
}

const IMAGE_VARS = ['--canvas-image', '--canvas-size', '--canvas-position', '--canvas-repeat']

export function applyWallpaper(url: string | null, config: WallpaperConfig = DEFAULT_WALLPAPER): void {
  const root = document.documentElement
  if (!url) {
    for (const name of IMAGE_VARS) root.style.removeProperty(name)
    return
  }
  const { aspect, fit, x, y, zoom } = config
  const scale = zoom / 100
  root.style.setProperty('--canvas-image', `url("${url}")`)
  root.style.setProperty('--canvas-position', `${x}% ${y}%`)
  root.style.setProperty('--canvas-repeat', fit === 'tile' ? 'repeat' : 'no-repeat')
  root.style.setProperty(
    '--canvas-size',
    fit === 'tile'
      // 平铺：高度先铺满，宽度按原图比例，横向多出来的空档由浏览器复制邻图补上
      ? `auto calc(100vh * ${scale})`
      // 铺满：由宽高比算出 cover 的尺寸，再整体乘缩放，任何比例都不会留白
      : `calc(max(100vw, ${round4(aspect * 100)}vh) * ${scale}) calc(max(${round4(100 / aspect)}vw, 100vh) * ${scale})`,
  )
}

function clearCustomVars(root: HTMLElement): void {
  for (const name of CUSTOM_INLINE_VARS) root.style.removeProperty(name)
}

function isDarkHex(hex: string): boolean {
  const raw = hex.trim().replace('#', '')
  const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw
  if (full.length !== 6) return false
  const r = Number.parseInt(full.slice(0, 2), 16)
  const g = Number.parseInt(full.slice(2, 4), 16)
  const b = Number.parseInt(full.slice(4, 6), 16)
  return (r * 299 + g * 587 + b * 114) / 1000 < 128
}

function round2(value: number): number {
  return Math.round(Math.min(1, Math.max(0, value)) * 100) / 100
}

/** 认 #rgb / #rrggbb / rgb() / rgba()：官方主题的底色两种写法都有 */
function parseColor(color: string): { r: number; g: number; b: number; a: number } | null {
  const text = color.trim()
  if (text.startsWith('#')) {
    const raw = text.slice(1)
    const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw
    if (full.length !== 6) return null
    return {
      r: Number.parseInt(full.slice(0, 2), 16),
      g: Number.parseInt(full.slice(2, 4), 16),
      b: Number.parseInt(full.slice(4, 6), 16),
      a: 1,
    }
  }
  const match = text.match(/^rgba?\(([^)]+)\)$/i)
  if (!match) return null
  const parts = match[1].split(',').map((part) => Number(part.trim()))
  if (parts.length < 3 || parts.some((value) => !Number.isFinite(value))) return null
  return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 }
}

function fadeColor(color: string, alpha: number): string | null {
  const rgb = parseColor(color)
  if (!rgb) return null
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${round2(rgb.a * alpha)})`
}

// 字体与字号对所有主题都生效（含官方三套）；默认档位与 tokens.css 原值一致，等于不改
function applyTypography(root: HTMLElement, style: ThemeStyle): void {
  const face = THEME_FONTS.find((item) => item.id === style.font) ?? THEME_FONTS[0]
  const size = THEME_FONT_SIZES.find((item) => item.id === style.fontSize) ?? THEME_FONT_SIZES[1]
  root.style.setProperty('--font-ui', face.css)
  root.style.setProperty('--font-editor', face.css)
  root.style.setProperty('--text-editor', size.editor)
  root.style.setProperty('--text-body', size.body)
  root.style.setProperty('--text-small', size.small)
  root.style.setProperty('--text-heading', size.heading)
}

function applyCustomPaint(root: HTMLElement, colors: CustomThemeTokens, alpha: number): void {
  const { canvas, ink, editor, editorInk, accent } = colors
  const fade = (color: string, level: number) => hexToRgba(color, round2(alpha * level))

  root.style.setProperty('--canvas', canvas)
  root.style.setProperty('--ink', ink)
  root.style.setProperty('--editor-ink', editorInk)
  root.style.setProperty('--accent', accent)
  root.style.setProperty('--heading', ink)

  for (const [name, level] of SHELL_LAYERS) root.style.setProperty(name, fade(editor, level))
  for (const [name, level] of INK_LAYERS) root.style.setProperty(name, fade(ink, level))
  for (const [name, level] of ACCENT_LAYERS) root.style.setProperty(name, fade(accent, level))

  root.style.setProperty('--accent-hover', shadeHex(accent, 0.9))
  root.style.setProperty('--accent-pressed', shadeHex(accent, 0.8))
  root.style.setProperty('--link-hover', shadeHex(accent, 0.9))
  root.style.setProperty('--focus', accent)
  root.style.setProperty('--btn-primary-border', shadeHex(accent, 0.82))
  root.style.setProperty('--btn-primary-ink', isDarkHex(accent) ? '#ffffff' : '#242019')
}

export function applyTheme(
  id: ThemeId,
  colors: CustomThemeTokens = readActivePreset()?.tokens ?? DEFAULT_CUSTOM_TOKENS,
  style: ThemeStyle = readThemeStyle(),
): void {
  const root = document.documentElement
  clearCustomVars(root)
  if (id !== 'custom') applyWallpaper(null)

  applyTypography(root, style)
  const alpha = clampChromeOpacity(style.chromeOpacity) / 100
  if (id === 'custom') {
    applyCustomPaint(root, colors, alpha)
  } else if (alpha < 1) {
    // 官方主题只在拉低透明度时才接管外壳底色，100% 时完全交回样式表
    for (const [name, color] of THEME_SHELLS[id]) {
      const faded = fadeColor(color, alpha)
      if (faded) root.style.setProperty(name, faded)
    }
  }

  if (id === 'paper') {
    root.removeAttribute('data-theme')
    root.style.colorScheme = 'light'
    return
  }
  root.setAttribute('data-theme', id)
  root.style.colorScheme = id === 'night'
    ? 'dark'
    : id === 'contrast'
      ? 'light'
      : isDarkHex(colors.canvas) ? 'dark' : 'light'
}
