import { expect, it } from 'vitest'
import { wallpaperError } from './themeImage'

it('accepts a small image and rejects the rest', () => {
  expect(wallpaperError(new File(['x'], 'a.png', { type: 'image/png' }))).toBeNull()
  expect(wallpaperError(new File(['x'], 'a.txt', { type: 'text/plain' }))).toBe('只要 jpg / png / webp / gif')
  const big = new File([new Uint8Array(2 * 1024 * 1024 + 1)], 'a.png', { type: 'image/png' })
  expect(wallpaperError(big)).toBe('图片不要超过 2 MB')
})
