// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { ThemeProvider } from '../context/ThemeContext'
import { api } from '../lib/api'
import { PRESET_STORAGE_KEY, STYLE_STORAGE_KEY, THEME_STORAGE_KEY } from '../lib/theme'
import AppearancePage from './AppearancePage'

vi.mock('../components/ProjectRail', () => ({ ProjectRail: () => <nav>项目</nav> }))
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ logout: vi.fn() }) }))
vi.mock('../lib/api', () => ({
  api: { listProjects: vi.fn() },
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  localStorage.clear()
  document.documentElement.removeAttribute('style')
  document.documentElement.removeAttribute('data-theme')
})

const TINY_PNG = Uint8Array.from(
  atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='),
  (char) => char.charCodeAt(0),
)

// useBlocker 只在 data router 下能用，所以测试也得用 createMemoryRouter
function renderPage() {
  vi.mocked(api.listProjects).mockResolvedValue([])
  const router = createMemoryRouter(
    [
      { path: '/theme', element: <AppearancePage /> },
      { path: '/projects', element: <p>作品库</p> },
    ],
    { initialEntries: ['/theme'] },
  )
  return render(
    <ThemeProvider>
      <RouterProvider router={router} />
    </ThemeProvider>,
  )
}

function cssVar(name: string): string {
  return document.documentElement.style.getPropertyValue(name)
}

function stage(): HTMLElement {
  return document.querySelector('[role="presentation"]') as HTMLElement
}

function storedPresets(): Array<Record<string, unknown>> {
  return (JSON.parse(localStorage.getItem(PRESET_STORAGE_KEY) || '{"items":[]}') as {
    items: Array<Record<string, unknown>>
  }).items
}

async function newPreset() {
  renderPage()
  fireEvent.click(await screen.findByRole('button', { name: '新建自定义' }))
}

it('opens a large preview and applies only after confirm', async () => {
  renderPage()
  fireEvent.click(await screen.findByRole('button', { name: '夜间' }))
  expect(document.documentElement.getAttribute('data-theme')).toBeNull()
  const dialog = screen.getByRole('dialog', { name: '夜间' })
  expect(dialog).toBeTruthy()
  expect(dialog.textContent).toMatch(/生成与重写/)
  expect(dialog.textContent).toMatch(/写下一章/)
  expect(dialog.textContent).toMatch(/章节流转/)
  fireEvent.click(screen.getByRole('button', { name: '先不应用' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.documentElement.getAttribute('data-theme')).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: '夜间' }))
  fireEvent.click(screen.getByRole('button', { name: '应用这套' }))
  expect(document.documentElement.getAttribute('data-theme')).toBe('night')
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('night')
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('previews a draft live on the whole page and reverts when it is dropped', async () => {
  await newPreset()
  // 新建只是草稿：还没点保存，盘上什么都没有
  expect(localStorage.getItem(PRESET_STORAGE_KEY)).toBeNull()
  expect(screen.queryByRole('button', { name: '自定义 1' })).toBeNull()

  // 但改动已经实时作用到整页，这就是「所见即所得」
  fireEvent.change(screen.getByRole('textbox', { name: '整页底' }), { target: { value: '#112233' } })
  expect(cssVar('--canvas')).toBe('#112233')

  fireEvent.click(screen.getByRole('button', { name: '收起' }))
  fireEvent.click(screen.getByRole('button', { name: '确认' }))
  await waitFor(() => {
    expect(cssVar('--canvas')).toBe('')
  })
  expect(localStorage.getItem(PRESET_STORAGE_KEY)).toBeNull()
})

it('asks before dropping an unsaved draft, and 取消 keeps it', async () => {
  await newPreset()
  fireEvent.change(screen.getByRole('textbox', { name: '整页底' }), { target: { value: '#112233' } })

  fireEvent.click(screen.getByRole('button', { name: '收起' }))
  expect(screen.getByRole('dialog', { name: '还没保存' })).toBeTruthy()

  // 取消就留在原地，草稿和实时预览都还在
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(screen.queryByRole('dialog', { name: '还没保存' })).toBeNull()
  expect(screen.getByRole('textbox', { name: '整页底' })).toBeTruthy()
  expect(cssVar('--canvas')).toBe('#112233')
})

it('does not nag when the editor was never touched', async () => {
  await newPreset()
  fireEvent.click(screen.getByRole('button', { name: '收起' }))
  expect(screen.queryByRole('dialog', { name: '还没保存' })).toBeNull()
  expect(screen.queryByRole('textbox', { name: '整页底' })).toBeNull()
})

it('blocks leaving the page while the font and opacity draft is unsaved', async () => {
  renderPage()
  await screen.findByRole('button', { name: '纸感' })
  fireEvent.click(screen.getByRole('button', { name: '楷体' }))

  fireEvent.click(screen.getByRole('link', { name: '返回作品库' }))
  expect(screen.getByRole('dialog', { name: '还没保存' })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(screen.queryByRole('dialog', { name: '还没保存' })).toBeNull()
  expect(screen.getByRole('button', { name: '楷体' })).toBeTruthy()

  // 确认之后才真的走掉，走的时候把预览还原成盘上那套
  fireEvent.click(screen.getByRole('link', { name: '返回作品库' }))
  fireEvent.click(screen.getByRole('button', { name: '确认' }))
  expect(await screen.findByText('作品库')).toBeTruthy()
  expect(cssVar('--font-ui')).toContain('sans-serif')
})

it('writes the preset and the chosen colors only on 保存这套', async () => {
  await newPreset()
  fireEvent.change(screen.getByRole('textbox', { name: '整页底' }), { target: { value: '#112233' } })
  fireEvent.click(screen.getByRole('button', { name: '保存这套' }))

  expect(document.documentElement.getAttribute('data-theme')).toBe('custom')
  expect(cssVar('--canvas')).toBe('#112233')
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('custom')
  expect(screen.getByRole('button', { name: '自定义 1' })).toBeTruthy()
  expect(storedPresets()).toHaveLength(1)
})

it('fades the paper and the selections to the chrome opacity, not just the chrome', async () => {
  await newPreset()
  // 默认不透明：纸就是纸的原色
  expect(cssVar('--editor')).toBe('rgba(255, 253, 247, 1)')

  fireEvent.change(screen.getByLabelText('界面透明度'), { target: { value: '40' } })
  expect(cssVar('--surface-1')).toBe('rgba(255, 253, 247, 0.34)')
  // 正文纸跟着同一个滑块，只是层级更高，靠亮度而不是不透明跟外壳区分
  expect(cssVar('--editor')).toBe('rgba(255, 253, 247, 0.4)')
  expect(cssVar('--accent-soft')).toBe('rgba(159, 216, 173, 0.24)')
})

it('keeps font and opacity edits as a draft until they are saved', async () => {
  await newPreset()
  fireEvent.change(screen.getByLabelText('界面透明度'), { target: { value: '40' } })
  fireEvent.click(screen.getByRole('button', { name: '大' }))
  expect(cssVar('--text-editor')).toBe('19px')
  expect(localStorage.getItem(STYLE_STORAGE_KEY)).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  expect(JSON.parse(localStorage.getItem(STYLE_STORAGE_KEY) || '{}')).toEqual({
    chromeOpacity: 40,
    font: 'hei',
    fontSize: 'l',
  })
})

it('hands the font and opacity to the official themes too', async () => {
  renderPage()
  fireEvent.click(await screen.findByRole('button', { name: '楷体' }))
  expect(cssVar('--font-editor')).toContain('Kaiti')
  // 正文偏好不再覆盖小字号界面文字。
  expect(cssVar('--font-ui')).toContain('sans-serif')
  expect(cssVar('--font-ui')).not.toContain('Kaiti')
  fireEvent.change(screen.getByLabelText('界面透明度'), { target: { value: '40' } })
  expect(cssVar('--surface-1')).toBe('rgba(255, 255, 252, 0.34)')

  fireEvent.click(screen.getByRole('button', { name: '还原' }))
  expect(cssVar('--font-editor')).toContain('Microsoft YaHei')
  expect(cssVar('--font-ui')).toContain('sans-serif')
  expect(cssVar('--surface-1')).toBe('')
})

it('deletes a preset only after a second confirm click', async () => {
  await newPreset()
  fireEvent.click(screen.getByRole('button', { name: '保存这套' }))
  expect(screen.getByRole('button', { name: '自定义 1' })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '删除这套' }))
  expect(screen.getByRole('button', { name: '确认删除' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '自定义 1' })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
  expect(screen.queryByRole('button', { name: '自定义 1' })).toBeNull()
  expect(storedPresets()).toHaveLength(0)
})

it('marks the theme already in use instead of offering to apply it again', async () => {
  await newPreset()
  fireEvent.click(screen.getByRole('button', { name: '保存这套' }))
  fireEvent.click(screen.getByRole('button', { name: '自定义 1' }))
  expect(screen.getByText('使用中')).toBeTruthy()
  expect((screen.getByRole('button', { name: '应用这套' }) as HTMLButtonElement).disabled).toBe(true)
})

it('pins every theme card to the 万古魔尊 workspace preview', async () => {
  renderPage()
  expect((await screen.findAllByText('万古魔尊')).length).toBeGreaterThan(0)
  expect(screen.getAllByText(/第 1 章/).length).toBeGreaterThan(0)
  expect(screen.getAllByText(/那道裂痕弯得太规整了/).length).toBeGreaterThan(0)
  expect(screen.getAllByText('设定').length).toBeGreaterThan(0)
  expect(screen.getAllByText('创作设置').length).toBeGreaterThan(0)
  expect(screen.getAllByText('全局审计').length).toBeGreaterThan(0)
  expect(screen.getAllByText('生成与重写').length).toBeGreaterThan(0)
  expect(screen.getAllByText('校正记忆').length).toBeGreaterThan(0)
  expect(screen.getAllByText(/写下一章/).length).toBeGreaterThan(0)
  expect(screen.getAllByText('章节流转').length).toBeGreaterThan(0)
})

it('rejects a non-image wallpaper on the appearance page', async () => {
  await newPreset()
  fireEvent.change(screen.getByLabelText('上传背景图'), {
    target: { files: [new File(['nope'], 'x.txt', { type: 'text/plain' })] },
  })
  expect(await screen.findByText('只要 jpg / png / webp / gif')).toBeTruthy()
})

it('previews a picked wallpaper live and still keeps it out of storage', async () => {
  await newPreset()
  fireEvent.change(screen.getByLabelText('上传背景图'), {
    target: { files: [new File([TINY_PNG], 'bg.png', { type: 'image/png' })] },
  })
  // 选完图不弹预览、不写盘，但整页和框里立刻能看到
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(cssVar('--canvas-image')).toMatch(/^url\(/)
  expect(stage().style.backgroundImage).toMatch(/^url\(/)
  expect(localStorage.getItem(PRESET_STORAGE_KEY)).toBeNull()
})

it('tiles the wallpaper instead of leaving blank bands, and zooms past the edges', async () => {
  await newPreset()
  fireEvent.change(screen.getByLabelText('上传背景图'), {
    target: { files: [new File([TINY_PNG], 'bg.png', { type: 'image/png' })] },
  })
  // 铺满（默认）：按 cover 算尺寸，任何比例都不留白
  expect(cssVar('--canvas-repeat')).toBe('no-repeat')
  expect(cssVar('--canvas-size')).toBe('calc(max(100vw, 177.7778vh) * 1) calc(max(56.25vw, 100vh) * 1)')

  fireEvent.click(screen.getByRole('button', { name: '平铺' }))
  expect(cssVar('--canvas-repeat')).toBe('repeat')
  expect(cssVar('--canvas-size')).toBe('auto calc(100vh * 1)')

  fireEvent.change(screen.getByLabelText('图片缩放'), { target: { value: '200' } })
  expect(cssVar('--canvas-size')).toBe('auto calc(100vh * 2)')
  expect(screen.getByText('200%')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '去掉' }))
  await waitFor(() => {
    expect(cssVar('--canvas-image')).toBe('')
  })
})
