// @vitest-environment jsdom
import { beforeEach, expect, it } from 'vitest'
import { getSession, SESSION_STORAGE_KEY } from './token'

beforeEach(() => localStorage.clear())

it('defaults a stored pre-role session to an unverified user role', () => {
  localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({
    token: 'old-token', userId: 'user-a', username: 'alice', tier: 'normal',
    expiresAt: Date.now() + 60_000,
  }))

  expect(getSession()).toMatchObject({ role: 'user', roleVerified: false })
})

it('never treats a locally stored admin role as verified', () => {
  localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({
    token: 'forged-token', userId: 'user-a', username: 'alice', tier: 'normal',
    role: 'admin', roleVerified: true, expiresAt: Date.now() + 60_000,
  }))

  expect(getSession()).toMatchObject({ role: 'admin', roleVerified: false })
})
