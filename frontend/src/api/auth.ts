import { ApiError, requestJson } from './client'

export interface Workspace { id: string; slug: string; name: string; status: 'active' | 'archived'; role: 'admin' | 'member' }
export interface Session { user: { id: string; username: string; display_name: string }; workspaces: Workspace[]; csrf_token: string; expires_at: string }
function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
export function parseSession(value: unknown): Session {
  if (!record(value) || !record(value.user) || typeof value.user.id !== 'string' || typeof value.user.username !== 'string' || typeof value.user.display_name !== 'string' || !Array.isArray(value.workspaces) || typeof value.csrf_token !== 'string' || typeof value.expires_at !== 'string') throw new ApiError('登录响应格式不正确', 502)
  for (const item of value.workspaces) {
    if (!record(item) || typeof item.id !== 'string' || typeof item.slug !== 'string' || typeof item.name !== 'string' || !['active', 'archived'].includes(String(item.status)) || !['admin', 'member'].includes(String(item.role))) throw new ApiError('工作空间响应格式不正确', 502)
  }
  return value as unknown as Session
}
export async function getSession(): Promise<Session> { return parseSession(await requestJson('/api/auth/me')) }
export async function login(username: string, password: string): Promise<Session> {
  return parseSession(await requestJson('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }))
}
export async function logout(): Promise<void> { await requestJson('/api/auth/logout', { method: 'POST' }) }
