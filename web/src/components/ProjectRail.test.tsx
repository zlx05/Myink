// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import type { Project } from '../types'
import { ProjectRail } from './ProjectRail'

const currentSession = vi.hoisted(() => ({ value: {
  username: 'alice', tier: 'normal', role: 'user' as 'user' | 'admin', roleVerified: true,
} }))

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ session: currentSession.value, status: 'authenticated' }),
}))

afterEach(() => {
  cleanup()
})

function renderRail(path: string, projects: Project[]) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/projects" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
        <Route path="/environment" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
        <Route path="/theme" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
        <Route path="/account" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
        <Route path="/admin" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
        <Route path="/appearance" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
        <Route path="/projects/:projectId" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
        <Route path="/projects/:projectId/settings" element={<ProjectRail projects={projects} onLogout={vi.fn()} />} />
      </Routes>
    </MemoryRouter>,
  )
}

it('shows environment and appearance only on global pages, not inside a book', () => {
  const projects: Project[] = [{ id: 'p1', title: '第一本书', genre: '都市', current_chapter: 1, target_words: 3000 }]

  const { unmount } = renderRail('/projects', projects)
  expect(screen.getByRole('link', { name: '环境配置' })).toBeTruthy()
  expect(screen.getByRole('link', { name: '主题' })).toBeTruthy()
  expect(screen.getByRole('link', { name: '账号' })).toBeTruthy()
  expect(screen.getByText('alice')).toBeTruthy()
  expect(screen.queryByRole('link', { name: '创作设置' })).toBeNull()
  unmount()

  const env = renderRail('/environment', projects)
  expect(screen.getByRole('link', { name: '环境配置' })).toBeTruthy()
  expect(screen.getByRole('link', { name: '主题' })).toBeTruthy()
  env.unmount()

  renderRail('/projects/p1', projects)
  expect(screen.queryByRole('link', { name: '环境配置' })).toBeNull()
  expect(screen.queryByRole('link', { name: '主题' })).toBeNull()
  expect(screen.getByRole('link', { name: '账号' })).toBeTruthy()
  expect(screen.getByRole('link', { name: '创作设置' })).toBeTruthy()
})

it('shows the admin entry only for a server-verified administrator', () => {
  const projects: Project[] = []
  const userView = renderRail('/projects', projects)
  expect(screen.queryByRole('link', { name: '管理后台' })).toBeNull()
  userView.unmount()

  currentSession.value = { ...currentSession.value, role: 'admin', roleVerified: false }
  const cachedView = renderRail('/projects', projects)
  expect(screen.queryByRole('link', { name: '管理后台' })).toBeNull()
  cachedView.unmount()

  currentSession.value = { ...currentSession.value, role: 'admin', roleVerified: true }
  renderRail('/projects', projects)
  expect(screen.getByRole('link', { name: '管理后台' })).toBeTruthy()
})
