import { afterEach, expect, it, vi } from 'vitest'
import { getSession, parseSession } from './auth'
import { requestJson, setSecurityContext } from './client'

afterEach(() => { vi.unstubAllGlobals(); setSecurityContext('', '') })
it('写操作携带 Cookie、CSRF 和空间范围，退出 204 不解析 JSON', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
  vi.stubGlobal('fetch', fetcher)
  setSecurityContext('csrf-value', 'workspace-id')
  expect(await requestJson('/api/auth/logout', { method: 'POST' })).toBeNull()
  expect(fetcher.mock.calls[0]?.[1]).toMatchObject({ credentials: 'include', headers: { 'X-CSRF-Token': 'csrf-value', 'X-Workspace-ID': 'workspace-id' } })
})
it('登录恢复拒绝格式不正确的响应', async () => {
  expect(() => parseSession({ user: { id: 'u1' } })).toThrow('格式不正确')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 200 })))
  await expect(getSession()).rejects.toThrow('格式不正确')
})
