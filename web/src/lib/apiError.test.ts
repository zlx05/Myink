// @vitest-environment node
import { expect, it } from 'vitest'
import { formatApiError, formatErrorText } from './apiError'
import { ApiError } from './api'

it('maps gateway codes and HTTP status to Chinese', () => {
  expect(formatApiError(new ApiError(429, 'CONCURRENCY_LIMIT', null))).toBe('本书已有进行中的任务，请稍候')
  expect(formatApiError(new ApiError(503, 'enqueue_failed', null))).toBe('写作任务没能进入队列，请稍后重试')
  expect(formatApiError(new ApiError(500, 'Internal Server Error', null))).toBe('服务器出错，请稍后重试')
  expect(formatApiError(new ApiError(500, '500', null))).toBe('服务器出错，请稍后重试')
  expect(formatApiError(new ApiError(404, 'not_found', null))).toBe('未找到相关内容')
  expect(formatErrorText('', 429)).toBe('请求过于频繁或额度已用完，请稍后再试')
  expect(formatErrorText('INVITATION_REQUIRED')).toBe('请输入邀请码')
  expect(formatErrorText('INVITATION_INVALID')).toBe('邀请码无效')
  expect(formatErrorText('INVITATION_EXPIRED')).toBe('邀请码已过期')
  expect(formatErrorText('INVITATION_REVOKED')).toBe('邀请码已被撤销')
  expect(formatErrorText('INVITATION_USED')).toBe('邀请码已用完')
})

it('maps model authentication failures to Chinese', () => {
  expect(formatErrorText(
    "Error code: 401 - {'error': {'message': 'Authentication Fails, Your api key: ****-xxx is invalid'}}",
  )).toBe('模型密钥无效。请到环境配置检查 API Key。')
})

it('keeps already-Chinese details and uses fallback for unknown errors', () => {
  expect(formatApiError(new ApiError(400, '扫榜超时须为 1–60 秒', null))).toBe('扫榜超时须为 1–60 秒')
  expect(formatApiError(new Error('boom'), '加载失败')).toBe('加载失败')
})
