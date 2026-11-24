import { ApiError, requestJson } from './client'

export interface InitialSetupStatus {
  available: boolean
  username: string | null
}

export interface InitialAdminInput {
  token: string
  username: string
  display_name: string
  password: string
}

export async function getInitialSetup(): Promise<InitialSetupStatus> {
  const data = await requestJson('/api/auth/setup')
  if (typeof data !== 'object' || data === null || !('available' in data) || !('username' in data)
    || typeof data.available !== 'boolean' || (data.username !== null && typeof data.username !== 'string')) {
    throw new ApiError('首次部署状态格式不正确', 502)
  }
  return { available: data.available, username: data.username }
}

export async function createInitialAdmin(input: InitialAdminInput): Promise<string> {
  const data = await requestJson('/api/auth/setup', { method: 'POST', body: JSON.stringify(input) })
  if (typeof data !== 'object' || data === null || !('username' in data) || typeof data.username !== 'string') {
    throw new ApiError('管理员创建结果格式不正确', 502)
  }
  return data.username
}
