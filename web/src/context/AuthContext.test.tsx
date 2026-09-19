// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import { dispatchUnauthorized, setSession } from '../lib/token'
import { AuthProvider, useAuth } from './AuthContext'

vi.mock('../lib/api', () => ({
  abortRequestsForToken: vi.fn(),
  api: {
    login: vi.fn(),
    register: vi.fn(),
    getSession: vi.fn(),
    logout: vi.fn(),
    changePassword: vi.fn(),
  },
}))

function Probe() {
  const auth = useAuth()
  return (
    <div>
      <span data-testid="status">{auth.status}</span>
      <span data-testid="identity">{auth.session?.username ?? 'signed-out'}</span>
      <span data-testid="role">{auth.session?.role ?? 'none'}:{String(auth.session?.roleVerified ?? false)}</span>
      <button type="button" onClick={() => void auth.login(' Alice ', 'correct horse battery')}>login</button>
      <button type="button" onClick={() => void auth.register('Alice', 'correct horse battery', 'invite-123')}>register</button>
      <button type="button" onClick={() => void auth.logout()}>logout</button>
    </div>
  )
}

it('forwards an invitation for registration without persisting it', async () => {
  vi.mocked(api.register).mockResolvedValue({
    token: 'token-a', user_id: 'user-a', username: 'alice', tier: 'normal', role: 'user', expires_in: 3600,
  })

  render(<AuthProvider><Probe /></AuthProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'register' }))

  await waitFor(() => expect(screen.getByTestId('identity').textContent).toBe('alice'))
  expect(api.register).toHaveBeenCalledWith('Alice', 'correct horse battery', 'invite-123')
  expect(localStorage.getItem('myink.session')).not.toContain('invite-123')
})

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

it('validates a stored session before exposing authenticated content', async () => {
  setSession({
    token: 'cached-token', userId: 'cached-id', username: 'cached-name', tier: 'normal', role: 'user', roleVerified: true,
    expiresAt: Date.now() + 60_000,
  })
  vi.mocked(api.getSession).mockResolvedValue({ user_id: 'server-id', username: 'alice', tier: 'normal', role: 'admin' })

  render(<AuthProvider><Probe /></AuthProvider>)

  expect(screen.getByTestId('status').textContent).toBe('checking')
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
  expect(screen.getByTestId('identity').textContent).toBe('alice')
  expect(screen.getByTestId('role').textContent).toBe('admin:true')
  expect(JSON.parse(localStorage.getItem('myink.session') ?? '{}').role).toBe('admin')
  expect(api.getSession).toHaveBeenCalledWith('cached-token', expect.any(AbortSignal))
})

it('uses the canonical account returned by password login', async () => {
  vi.mocked(api.login).mockResolvedValue({
    token: 'token-a', user_id: 'user-a', username: 'alice', tier: 'normal', role: 'user', expires_in: 3600,
  })

  render(<AuthProvider><Probe /></AuthProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'login' }))

  await waitFor(() => expect(screen.getByTestId('identity').textContent).toBe('alice'))
  expect(api.login).toHaveBeenCalledWith(' Alice ', 'correct horse battery')
})

it('discards a late login response after another tab switches accounts', async () => {
  let resolveLogin!: (value: {
    token: string; user_id: string; username: string; tier: string; role: 'user' | 'admin'; expires_in: number
  }) => void
  vi.mocked(api.login).mockReturnValue(new Promise((resolve) => { resolveLogin = resolve }))
  vi.mocked(api.getSession).mockResolvedValue({ user_id: 'user-b', username: 'bob', tier: 'normal', role: 'user' })

  render(<AuthProvider><Probe /></AuthProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'login' }))

  const bob = {
    token: 'token-b', userId: 'user-b', username: 'bob', tier: 'normal', role: 'user' as const, roleVerified: true,
    expiresAt: Date.now() + 60_000,
  }
  localStorage.setItem('myink.session', JSON.stringify(bob))
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'myink.session', newValue: JSON.stringify(bob),
    }))
  })
  await waitFor(() => expect(screen.getByTestId('identity').textContent).toBe('bob'))

  await act(async () => {
    resolveLogin({
      token: 'token-a', user_id: 'user-a', username: 'alice', tier: 'normal', role: 'user', expires_in: 3600,
    })
  })
  expect(screen.getByTestId('identity').textContent).toBe('bob')
})

it('adopts storage instead of letting a late login overwrite an unseen account switch', async () => {
  let resolveLogin!: (value: {
    token: string; user_id: string; username: string; tier: string; role: 'user' | 'admin'; expires_in: number
  }) => void
  vi.mocked(api.login).mockReturnValue(new Promise((resolve) => { resolveLogin = resolve }))
  vi.mocked(api.getSession).mockResolvedValue({ user_id: 'user-b', username: 'bob', tier: 'normal', role: 'user' })

  render(<AuthProvider><Probe /></AuthProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'login' }))
  const bob = {
    token: 'token-b', userId: 'user-b', username: 'bob', tier: 'normal', role: 'user', roleVerified: true,
    expiresAt: Date.now() + 60_000,
  }
  localStorage.setItem('myink.session', JSON.stringify(bob))

  await act(async () => {
    resolveLogin({
      token: 'token-a', user_id: 'user-a', username: 'alice', tier: 'normal', role: 'user', expires_in: 3600,
    })
  })

  await waitFor(() => expect(screen.getByTestId('identity').textContent).toBe('bob'))
  expect(JSON.parse(localStorage.getItem('myink.session') ?? '{}').token).toBe('token-b')
})

it('ignores a delayed unauthorized event from the previous token', async () => {
  const bob = {
    token: 'token-b', userId: 'user-b', username: 'bob', tier: 'normal', role: 'user' as const, roleVerified: true,
    expiresAt: Date.now() + 60_000,
  }
  setSession(bob)
  vi.mocked(api.getSession).mockResolvedValue({ user_id: 'user-b', username: 'bob', tier: 'normal', role: 'user' })
  render(<AuthProvider><Probe /></AuthProvider>)
  await waitFor(() => expect(screen.getByTestId('identity').textContent).toBe('bob'))

  act(() => dispatchUnauthorized('token-a'))

  expect(screen.getByTestId('identity').textContent).toBe('bob')
})

it('clears an authenticated session when its local expiry time arrives', async () => {
  setSession({
    token: 'short-token', userId: 'user-a', username: 'alice', tier: 'normal', role: 'user', roleVerified: true,
    expiresAt: Date.now() + 50,
  })
  vi.mocked(api.getSession).mockResolvedValue({ user_id: 'user-a', username: 'alice', tier: 'normal', role: 'user' })
  render(<AuthProvider><Probe /></AuthProvider>)

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
  await waitFor(() => expect(screen.getByTestId('identity').textContent).toBe('signed-out'), { timeout: 1000 })
  expect(localStorage.getItem('myink.session')).toBeNull()
})

it('does not let an old expiry timer erase a newer token before its storage event arrives', async () => {
  setSession({
    token: 'token-a', userId: 'user-a', username: 'alice', tier: 'normal', role: 'user', roleVerified: true,
    expiresAt: Date.now() + 80,
  })
  vi.mocked(api.getSession).mockImplementation(async (token: string) => token === 'token-a'
    ? { user_id: 'user-a', username: 'alice', tier: 'normal', role: 'user' }
    : { user_id: 'user-b', username: 'bob', tier: 'normal', role: 'user' })
  render(<AuthProvider><Probe /></AuthProvider>)
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

  const bob = {
    token: 'token-b', userId: 'user-b', username: 'bob', tier: 'normal', role: 'user', roleVerified: true,
    expiresAt: Date.now() + 60_000,
  }
  localStorage.setItem('myink.session', JSON.stringify(bob))

  await waitFor(() => expect(screen.getByTestId('identity').textContent).toBe('bob'), { timeout: 1000 })
  expect(JSON.parse(localStorage.getItem('myink.session') ?? '{}').token).toBe('token-b')
})
