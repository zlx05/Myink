// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { useAuth } from '../context/AuthContext'
import LoginPage from './LoginPage'

vi.mock('../context/AuthContext', () => ({ useAuth: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('requires matching registration passwords before submitting credentials', () => {
  const register = vi.fn()
  vi.mocked(useAuth).mockReturnValue({ register } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter><LoginPage /></MemoryRouter>)

  fireEvent.click(screen.getByRole('tab', { name: '注册账号' }))
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'Alice' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'correct horse battery 1' } })
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'different password 2' } })
  fireEvent.click(screen.getByRole('button', { name: '创建账号' }))

  expect(screen.getByText('两次输入的密码不一致')).toBeTruthy()
  expect(register).not.toHaveBeenCalled()
})

it('shows an invitation only for registration and submits it without storing it', async () => {
  const register = vi.fn().mockResolvedValue(false)
  vi.mocked(useAuth).mockReturnValue({ register } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter><LoginPage /></MemoryRouter>)

  expect(screen.queryByLabelText('邀请码')).toBeNull()
  fireEvent.click(screen.getByRole('tab', { name: '注册账号' }))
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'Alice' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'correct horse battery 1' } })
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'correct horse battery 1' } })
  fireEvent.change(screen.getByLabelText('邀请码'), { target: { value: '  invitation-secret  ' } })
  fireEvent.click(screen.getByRole('button', { name: '创建账号' }))

  await screen.findByRole('button', { name: '创建账号' })
  expect(register).toHaveBeenCalledWith('alice', 'correct horse battery 1', 'invitation-secret')
  expect(JSON.stringify(localStorage)).not.toContain('invitation-secret')

  fireEvent.click(screen.getByRole('tab', { name: '登录' }))
  expect(screen.queryByLabelText('邀请码')).toBeNull()
})

it('requires an invitation before registration submission', () => {
  const register = vi.fn()
  vi.mocked(useAuth).mockReturnValue({ register } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter><LoginPage /></MemoryRouter>)

  fireEvent.click(screen.getByRole('tab', { name: '注册账号' }))
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'Alice' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'correct horse battery 1' } })
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'correct horse battery 1' } })
  fireEvent.click(screen.getByRole('button', { name: '创建账号' }))

  expect(screen.getByText('请输入邀请码')).toBeTruthy()
  expect(register).not.toHaveBeenCalled()
})

it('rejects Unicode characters that case-fold into ASCII usernames', () => {
  const login = vi.fn()
  vi.mocked(useAuth).mockReturnValue({ login } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter><LoginPage /></MemoryRouter>)

  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'Kaa' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'correct horse battery' } })
  fireEvent.click(screen.getByRole('button', { name: '登录' }))

  expect(screen.getByText('用户名需为 3–64 位英文字母、数字、下划线或短横线')).toBeTruthy()
  expect(login).not.toHaveBeenCalled()
})

it('allows a legacy short password for login without applying new-password rules', async () => {
  const login = vi.fn().mockResolvedValue(false)
  vi.mocked(useAuth).mockReturnValue({ login } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter><LoginPage /></MemoryRouter>)

  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'Alice' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'oldpass' } })
  fireEvent.click(screen.getByRole('button', { name: '登录' }))

  await screen.findByRole('button', { name: '登录' })
  expect(login).toHaveBeenCalledWith('alice', 'oldpass')
})

it('requires an ASCII letter and digit in an eight-character registration password', async () => {
  const register = vi.fn().mockResolvedValue(false)
  vi.mocked(useAuth).mockReturnValue({ register } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter><LoginPage /></MemoryRouter>)

  fireEvent.click(screen.getByRole('tab', { name: '注册账号' }))
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'Alice' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'abcdefgh' } })
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'abcdefgh' } })
  fireEvent.change(screen.getByLabelText('邀请码'), { target: { value: 'invite' } })
  fireEvent.click(screen.getByRole('button', { name: '创建账号' }))
  expect(screen.getByRole('alert').textContent).toContain('ASCII 字母和 1 个 ASCII 数字')
  expect(register).not.toHaveBeenCalled()

  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'abcd1234' } })
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'abcd1234' } })
  fireEvent.click(screen.getByRole('button', { name: '创建账号' }))
  await screen.findByRole('button', { name: '创建账号' })
  expect(register).toHaveBeenCalledWith('alice', 'abcd1234', 'invite')
  expect(screen.getByText(/administrator/i).textContent).toContain('writing and debugging content')
})
