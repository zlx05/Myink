// @vitest-environment jsdom
// 设定页台账两块（批次 2）：事件台账（§7.4）渲染摘要与人名；人物状态变化时间线（§7.7）
// 首次展开才懒加载、且**已失效行也在**（这正是新增接口存在的理由，压平后看不到）。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import LorePage from './LorePage'
import { api } from '../lib/api'
import type { CharacterCard, StoryEvent, CharacterStateChange } from '../types'

vi.mock('../components/ProjectRail', () => ({ ProjectRail: () => <nav>项目</nav> }))
vi.mock('../components/WorldGraph', () => ({ WorldGraph: () => <div>图谱</div> }))
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ logout: vi.fn() }) }))
vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error { code = 'API_ERROR' },
  api: {
    listProjects: vi.fn(), getWorld: vi.fn(), getCharacters: vi.fn(),
    listEntities: vi.fn(), listForeshadows: vi.fn(), listChapters: vi.fn(),
    getOutline: vi.fn(), listEvents: vi.fn(), getCharacterStateHistory: vi.fn(),
  },
}))

// 卡片名与事件参与人取不同人名，避免 getByText 命中两处
const CARD: CharacterCard = {
  id: 'c1', name: '顾长歌', race: '人族', origin: null, realm_cap: '金丹',
  personality: null, base_attrs: {}, state: { realm: '金丹初期' },
}

const EVENTS: StoryEvent[] = [
  { id: 'e2', summary: '沈岳夜访秘境', participants: ['沈岳'],
    source_chapter: 7, confidence: 0.8 },
  { id: 'e1', summary: '林晚于青云山夺剑', participants: ['林晚'],
    source_chapter: 3, confidence: 0.9 },
]

const HISTORY: CharacterStateChange[] = [
  { field: 'realm', old_value: '练气九层', new_value: '筑基一层', chapter_seq: 2,
    source_chapter: 2, confidence: 0.9, valid_from: 2, valid_to: null },
  // 已失效行（valid_to 非空）：get_character_state 会把它丢掉，端点与 UI 都不能丢
  { field: 'realm', old_value: '筑基一层', new_value: '筑基三层', chapter_seq: 3,
    source_chapter: 3, confidence: 0.8, valid_from: 3, valid_to: 5 },
  { field: 'realm', old_value: '筑基三层', new_value: '金丹初期', chapter_seq: 5,
    source_chapter: 5, confidence: 0.95, valid_from: 5, valid_to: null },
]

function seedPage() {
  vi.mocked(api.listProjects).mockResolvedValue([])
  vi.mocked(api.getWorld).mockResolvedValue({
    world_rules: {}, hard_constraints: [], factions: [], locations: [],
  })
  vi.mocked(api.getCharacters).mockResolvedValue([CARD])
  vi.mocked(api.listEntities).mockResolvedValue([])
  vi.mocked(api.listForeshadows).mockResolvedValue([])
  vi.mocked(api.listChapters).mockResolvedValue([])
  vi.mocked(api.getOutline).mockResolvedValue({ outline: null })
  vi.mocked(api.listEvents).mockResolvedValue([])
  vi.mocked(api.getCharacterStateHistory).mockResolvedValue([])
}

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/projects/p/lore']}>
      <Routes><Route path="/projects/:projectId/lore" element={<LorePage />} /></Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('renders the event ledger with summaries and participant names', async () => {
  seedPage()
  vi.mocked(api.listEvents).mockResolvedValue(EVENTS)

  renderPage()

  expect(await screen.findByText('事件台账')).toBeTruthy()
  expect(screen.getByText('林晚于青云山夺剑')).toBeTruthy()
  expect(screen.getByText('林晚')).toBeTruthy()
  expect(screen.getByText('沈岳')).toBeTruthy()
})

it('loads the state-change timeline on first expand, invalidated rows included', async () => {
  seedPage()
  vi.mocked(api.getCharacterStateHistory).mockResolvedValue(HISTORY)

  renderPage()

  // 未展开不拉数据（一次打 N 个请求不值当）
  expect(api.getCharacterStateHistory).not.toHaveBeenCalled()

  fireEvent.click(await screen.findByRole('button', { name: /顾长歌/ }))

  expect(await screen.findByText('练气九层 → 筑基一层')).toBeTruthy()
  expect(screen.getByText('筑基一层 → 筑基三层')).toBeTruthy()
  expect(screen.getByText('筑基三层 → 金丹初期')).toBeTruthy()
  expect(vi.mocked(api.getCharacterStateHistory).mock.calls).toEqual([['p', 'c1']])

  // 收起再展开不重复拉（loaded 兼作只拉一次闸）
  fireEvent.click(screen.getByRole('button', { name: /顾长歌/ }))
  fireEvent.click(screen.getByRole('button', { name: /顾长歌/ }))
  await screen.findByText('练气九层 → 筑基一层')
  expect(vi.mocked(api.getCharacterStateHistory).mock.calls).toEqual([['p', 'c1']])
})
