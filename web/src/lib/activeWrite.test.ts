// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { clearActiveWrite, readActiveWrite, writeActiveWrite } from './activeWrite'

afterEach(() => {
  sessionStorage.clear()
})

it('remembers an in-flight write per book', () => {
  writeActiveWrite('book-a', { taskId: 'task-2', chapterSeq: 2, batchTotal: null })
  writeActiveWrite('book-b', { taskId: 'task-1', chapterSeq: 1, batchTotal: 3 })
  expect(readActiveWrite('book-a')).toEqual({ taskId: 'task-2', chapterSeq: 2, batchTotal: null })
  expect(readActiveWrite('book-b')).toEqual({ taskId: 'task-1', chapterSeq: 1, batchTotal: 3 })
  clearActiveWrite('book-a')
  expect(readActiveWrite('book-a')).toBeNull()
  expect(readActiveWrite('book-b')?.taskId).toBe('task-1')
})

it('ignores a broken payload instead of throwing', () => {
  sessionStorage.setItem('aiink.activeWrite.book-a', '{')
  expect(readActiveWrite('book-a')).toBeNull()
})
