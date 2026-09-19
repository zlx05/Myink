// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api'
import AccountPage from './AccountPage'

vi.mock('../context/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('../lib/api', async (original) => {
  const actual = await original<typeof import('../lib/api')>()
  return { ...actual, api: { ...actual.api, listProjects: vi.fn() } }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('changes the current account password without persisting password fields', async () => {
  const changePassword = vi.fn().mockResolvedValue(undefined)
  vi.mocked(useAuth).mockReturnValue({
    session: {
      token: 'token-a', userId: 'user-a', username: 'alice', tier: 'normal', role: 'user', roleVerified: true, expiresAt: Date.now() + 60_000,
    },
    changePassword,
    logout: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
  vi.mocked(api.listProjects).mockResolvedValue([])

  render(<MemoryRouter><AccountPage /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('当前密码'), { target: { value: 'old password value' } })
  fireEvent.change(screen.getByLabelText('新密码'), { target: { value: 'new password value 1' } })
  fireEvent.change(screen.getByLabelText('确认新密码'), { target: { value: 'new password value 1' } })
  fireEvent.click(screen.getByRole('button', { name: '修改密码' }))

  await waitFor(() => expect(changePassword).toHaveBeenCalledWith('old password value', 'new password value 1'))
  expect(localStorage.getItem('myink.password')).toBeNull()
})

it('rejects a new password without both an ASCII letter and digit', () => {
  const changePassword = vi.fn()
  vi.mocked(useAuth).mockReturnValue({
    session: {
      token: 'token-a', userId: 'user-a', username: 'alice', tier: 'normal', role: 'user', roleVerified: true,
      expiresAt: Date.now() + 60_000,
    },
    changePassword,
    logout: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
  vi.mocked(api.listProjects).mockResolvedValue([])

  render(<MemoryRouter><AccountPage /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('当前密码'), { target: { value: 'oldpass' } })
  fireEvent.change(screen.getByLabelText('新密码'), { target: { value: 'abcdefgh' } })
  fireEvent.change(screen.getByLabelText('确认新密码'), { target: { value: 'abcdefgh' } })
  fireEvent.click(screen.getByRole('button', { name: '修改密码' }))

  expect(screen.getByRole('alert').textContent).toContain('ASCII 字母和 1 个 ASCII 数字')
  expect(changePassword).not.toHaveBeenCalled()
})

it('offers the admin console only to a verified administrator', () => {
  vi.mocked(useAuth).mockReturnValue({
    session: {
      token: 'token-a', userId: 'admin-a', username: 'root', tier: 'normal', role: 'admin', roleVerified: true,
      expiresAt: Date.now() + 60_000,
    },
    status: 'authenticated',
    changePassword: vi.fn(),
    logout: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
  vi.mocked(api.listProjects).mockResolvedValue([])

  render(<MemoryRouter><AccountPage /></MemoryRouter>)

  expect(screen.getByRole('link', { name: '打开管理后台' }).getAttribute('href')).toBe('/admin')
})
