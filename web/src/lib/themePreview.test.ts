import { expect, it } from 'vitest'
import { PINNED_WORKSPACE_PREVIEW } from './themePreview'

it('pins the workspace preview to 万古魔尊 chapter one', () => {
  expect(PINNED_WORKSPACE_PREVIEW.book).toBe('万古魔尊')
  expect(PINNED_WORKSPACE_PREVIEW.title).toBe('第 1 章')
  expect(PINNED_WORKSPACE_PREVIEW.bookLinks).toEqual(['设定', '创作设置', '全局审计'])
  expect(PINNED_WORKSPACE_PREVIEW.chapters).toHaveLength(2)
  expect(PINNED_WORKSPACE_PREVIEW.body.startsWith('那道裂痕弯得太规整了')).toBe(true)
})
