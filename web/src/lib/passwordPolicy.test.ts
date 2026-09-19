import { describe, expect, it } from 'vitest'
import { isValidNewPassword } from './passwordPolicy'

describe('new-password policy', () => {
  it.each([
    'a1测试✅!!!',
    `a1${'§'.repeat(126)}`,
  ])('accepts valid Unicode and symbol passwords at the inclusive boundaries', (password) => {
    expect(isValidNewPassword(password)).toBe(true)
  })

  it.each([
    'a1short',
    `a1${'§'.repeat(127)}`,
    'abcdefgh',
    '12345678',
    '密码123456',
    'abcdefg١',
  ])('rejects out-of-range or missing ASCII letter/digit passwords', (password) => {
    expect(isValidNewPassword(password)).toBe(false)
  })
})
