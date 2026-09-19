// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectsPage from './ProjectsPage'

vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ logout: vi.fn(), session: { username: 'tester' } }) }))
vi.mock('../lib/api', async (original) => ({
  ...await original<typeof import('../lib/api')>(),
  api: { listProjects: vi.fn().mockResolvedValue([
    { id: 'ready', title: '正式书', genre: '悬疑', current_chapter: 0, creation_status: 'ready' },
    { id: 'draft', title: '未完成书', genre: '悬疑', current_chapter: 0, creation_status: 'draft' },
  ]) },
}))
afterEach(cleanup)

it('keeps unfinished books out of the writing rail and resumes them from a separate section', async () => {
  render(<MemoryRouter><ProjectsPage /></MemoryRouter>)
  const drafts = await screen.findByRole('region', { name: '待完成作品' })
  expect(within(drafts).getByRole('link').getAttribute('href')).toBe('/projects/new?draft=draft')
  expect(within(screen.getByRole('navigation', { name: '作品列表' })).queryByText('未完成书')).toBeNull()
  expect(within(screen.getByRole('navigation', { name: '作品列表' })).getByText('正式书')).toBeTruthy()
})
