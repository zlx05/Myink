export const NEW_PASSWORD_VALIDATION_MESSAGE = '密码需为 8–128 个字符，且至少包含 1 个 ASCII 字母和 1 个 ASCII 数字'

export function isValidNewPassword(value: string): boolean {
  const length = Array.from(value).length
  return length >= 8 && length <= 128 && /[A-Za-z]/.test(value) && /[0-9]/.test(value)
}
