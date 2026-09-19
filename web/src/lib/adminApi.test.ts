// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { adminApi } from './adminApi'

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('myink.session', JSON.stringify({
    token: 'token-a', userId: 'admin-a', username: 'root', tier: 'normal', role: 'admin',
    roleVerified: true, expiresAt: Date.now() + 60_000,
  }))
  vi.restoreAllMocks()
})

it('encodes admin list filters and uses the authenticated request path', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    items: [], total: 0, limit: 25, offset: 25,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchMock)

  await adminApi.listProjects('token-a', {
    userId: 'user/a', q: '书 名&一', limit: 25, offset: 25,
  })

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/admin/projects?user_id=user%2Fa&q=%E4%B9%A6+%E5%90%8D%26%E4%B8%80&limit=25&offset=25',
    expect.objectContaining({
      method: 'GET',
      headers: expect.objectContaining({ Authorization: 'Bearer token-a' }),
    }),
  )
})

it('passes an abort signal through detail requests', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 7 }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)
  const controller = new AbortController()

  await adminApi.getRun('token-a', 7, controller.signal)

  expect(fetchMock).toHaveBeenCalledWith('/api/v1/admin/runs/7', expect.objectContaining({
    signal: expect.any(AbortSignal),
  }))
})
