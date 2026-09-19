// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from './api'

beforeEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

it('sends username and password to the token endpoint', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    token: 'token-a',
    user_id: 'user-a',
    username: 'alice',
    tier: 'normal',
    expires_in: 3600,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchMock)

  await api.login('Alice', 'correct horse battery')

  expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/token', expect.objectContaining({
    method: 'POST',
    body: JSON.stringify({ username: 'Alice', password: 'correct horse battery' }),
  }))
})

it('sends the invitation only with registration credentials', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    token: 'token-a', user_id: 'user-a', username: 'alice', tier: 'normal', expires_in: 3600,
  }), { status: 201, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchMock)

  await api.register('alice', 'correct horse battery', 'invite-code-123')

  expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/register', expect.objectContaining({
    method: 'POST',
    body: JSON.stringify({
      username: 'alice',
      password: 'correct horse battery',
      invitation_code: 'invite-code-123',
    }),
  }))
})

it('does not globally sign out when the current password is rejected', async () => {
  localStorage.setItem('myink.session', JSON.stringify({
    token: 'token-b', userId: 'user-b', username: 'bob', tier: 'normal', expiresAt: Date.now() + 60_000,
  }))
  const events = vi.fn()
  window.addEventListener('myink:unauthorized', events)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ error: 'INVALID_CREDENTIALS' }),
    { status: 401, headers: { 'Content-Type': 'application/json' } },
  )))

  await expect(api.changePassword('wrong password', 'a much better password', 'token-b')).rejects.toMatchObject({
    status: 401,
    code: 'INVALID_CREDENTIALS',
  })
  expect(events).not.toHaveBeenCalled()
  window.removeEventListener('myink:unauthorized', events)
})

it('discards a successful protected response after the browser switches tokens', async () => {
  localStorage.setItem('myink.session', JSON.stringify({
    token: 'token-a', userId: 'user-a', username: 'alice', tier: 'normal', expiresAt: Date.now() + 60_000,
  }))
  let resolveBody!: (value: unknown) => void
  const body = new Promise((resolve) => { resolveBody = resolve })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => body,
  }))

  const pending = api.listProjects()
  localStorage.setItem('myink.session', JSON.stringify({
    token: 'token-b', userId: 'user-b', username: 'bob', tier: 'normal', expiresAt: Date.now() + 60_000,
  }))
  resolveBody([])

  await expect(pending).rejects.toMatchObject({ code: 'request_aborted' })
})
