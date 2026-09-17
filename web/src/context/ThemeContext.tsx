import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  applyTheme,
  applyWallpaper,
  PRESET_LIMIT,
  nextPresetName,
  readActivePreset,
  readPresetStore,
  readTheme,
  readThemeStyle,
  writePresetStore,
  writeTheme,
  writeThemeStyle,
  type CustomPreset,
  type CustomThemeTokens,
  type ThemeId,
  type ThemeStyle,
  type WallpaperConfig,
} from '../lib/theme'
import { clearWallpaper, readWallpaper } from '../lib/themeImage'

interface ThemeContextValue {
  theme: ThemeId
  presets: CustomPreset[]
  activePresetId: string | null
  style: ThemeStyle
  wallpaperUrl: string | null
  setTheme: (id: Exclude<ThemeId, 'custom'>) => void
  selectPreset: (id: string) => void
  savePreset: (id: string, name: string, tokens: CustomThemeTokens, wallpaper: WallpaperConfig) => void
  deletePreset: (id: string) => void
  saveStyle: (style: ThemeStyle) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const objectUrl = useRef<string | null>(null)
  const [theme, setThemeState] = useState<ThemeId>(() => {
    const initial = readTheme()
    applyTheme(initial, readActivePreset()?.tokens, readThemeStyle())
    return initial
  })
  const [store, setStore] = useState(() => readPresetStore())
  const [style, setStyleState] = useState<ThemeStyle>(() => readThemeStyle())
  const [wallpaperUrl, setWallpaperUrl] = useState<string | null>(null)

  const dropUrl = useCallback(() => {
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    objectUrl.current = null
    setWallpaperUrl(null)
  }, [])

  const showWallpaper = useCallback(async (id: string | null, apply: boolean) => {
    dropUrl()
    const preset = id ? readPresetStore().items.find((item) => item.id === id) ?? null : null
    const blob = preset ? await readWallpaper(preset.id) : null
    if (!blob) {
      if (apply) applyWallpaper(null)
      return
    }
    const url = URL.createObjectURL(blob)
    objectUrl.current = url
    setWallpaperUrl(url)
    if (apply) applyWallpaper(url, preset?.wallpaper)
  }, [dropUrl])

  useEffect(() => {
    const current = readPresetStore()
    void showWallpaper(readTheme() === 'custom' ? current.activeId : null, readTheme() === 'custom')
    return () => {
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    }
  }, [showWallpaper])

  const persist = useCallback((next: typeof store, nextTheme: ThemeId) => {
    writePresetStore(next)
    writeTheme(nextTheme)
    setStore(next)
    setThemeState(nextTheme)
  }, [])

  const setTheme = useCallback((id: Exclude<ThemeId, 'custom'>) => {
    applyTheme(id, undefined, style)
    persist(readPresetStore(), id)
    void showWallpaper(null, true)
  }, [persist, showWallpaper, style])

  const selectPreset = useCallback((id: string) => {
    const current = readPresetStore()
    const preset = current.items.find((item) => item.id === id)
    if (!preset) return
    const next = { items: current.items, activeId: id }
    applyTheme('custom', preset.tokens, style)
    persist(next, 'custom')
    void showWallpaper(id, true)
  }, [persist, showWallpaper, style])

  // 新建的预设只在编辑器的草稿里存在，直到「保存这套」才第一次写盘；所以不存在的 id 要能插入。
  const savePreset = useCallback((
    id: string,
    name: string,
    tokens: CustomThemeTokens,
    wallpaper: WallpaperConfig,
  ) => {
    const current = readPresetStore()
    const exists = current.items.some((item) => item.id === id)
    if (!exists && current.items.length >= PRESET_LIMIT) return
    const entry: CustomPreset = {
      id,
      name: name.trim() || nextPresetName(current.items),
      tokens,
      wallpaper,
    }
    const items = exists
      ? current.items.map((item) => (item.id === id ? entry : item))
      : [...current.items, entry]
    const next = { items, activeId: id }
    applyTheme('custom', tokens, style)
    persist(next, 'custom')
    void showWallpaper(id, true)
  }, [persist, showWallpaper, style])

  // 字体与透明度是账号级的：官方三套一样跟着走，所以存完要按当前主题重新铺一遍
  const saveStyle = useCallback((next: ThemeStyle) => {
    writeThemeStyle(next)
    setStyleState(next)
    const current = readPresetStore()
    applyTheme(readTheme(), current.items.find((item) => item.id === current.activeId)?.tokens, next)
  }, [])

  const deletePreset = useCallback((id: string) => {
    const current = readPresetStore()
    const items = current.items.filter((item) => item.id !== id)
    const activeId = current.activeId === id ? items[0]?.id ?? null : current.activeId
    const next = { items, activeId }
    writePresetStore(next)
    setStore(next)
    void clearWallpaper(id)
    if (readTheme() !== 'custom' || current.activeId !== id) return
    if (!activeId) {
      applyTheme('paper', undefined, style)
      writeTheme('paper')
      setThemeState('paper')
      void showWallpaper(null, true)
      return
    }
    const preset = items.find((item) => item.id === activeId)
    if (!preset) return
    applyTheme('custom', preset.tokens, style)
    writeTheme('custom')
    void showWallpaper(activeId, true)
  }, [showWallpaper, style])

  const value = useMemo(
    () => ({
      theme,
      presets: store.items,
      activePresetId: store.activeId,
      style,
      wallpaperUrl,
      setTheme,
      selectPreset,
      savePreset,
      deletePreset,
      saveStyle,
    }),
    [theme, store, style, wallpaperUrl, setTheme, selectPreset, savePreset, deletePreset, saveStyle],
  )
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme 必须在 ThemeProvider 内使用')
  return ctx
}
