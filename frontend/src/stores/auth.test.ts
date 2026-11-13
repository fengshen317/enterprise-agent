import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from './auth'
import * as api from '../api/auth'
import { ApiError, getWorkspaceId, setSecurityContext } from '../api/client'

vi.mock('../api/auth', () => ({ getSession: vi.fn(), login: vi.fn(), logout: vi.fn() }))
const session: api.Session = { user: { id: 'u1', username: 'alice', display_name: '甲' }, csrf_token: 'csrf-memory-only', expires_at: '2099-01-01', workspaces: [{ id: 'w1', name: '空间甲', slug: 'first', status: 'active', role: 'admin' }, { id: 'w2', name: '空间乙', slug: 'second', status: 'active', role: 'member' }] }
beforeEach(() => { setActivePinia(createPinia()); localStorage.clear(); vi.resetAllMocks(); setSecurityContext('', '') })
it('登录和空间选择只持久化公开空间标识，退出清空安全上下文', async () => {
  vi.mocked(api.login).mockResolvedValue(session)
  vi.mocked(api.logout).mockResolvedValue()
  const auth = useAuthStore()
  await auth.signIn('alice', 'not-stored-password')
  expect(auth.authenticated).toBe(true)
  expect(getWorkspaceId()).toBe('w1')
  auth.selectWorkspace('w2')
  expect(getWorkspaceId()).toBe('w2')
  expect(localStorage.getItem('ea-workspace:u1')).toBe('w2')
  expect(JSON.stringify(localStorage)).not.toContain('csrf-memory-only')
  expect(JSON.stringify(localStorage)).not.toContain('not-stored-password')
  expect(() => auth.selectWorkspace('foreign')).toThrow('不可访问')
  await auth.signOut()
  expect(auth.session).toBeNull()
  expect(getWorkspaceId()).toBe('')
})
it('退出后迟到的登录恢复响应不会恢复旧用户', async () => {
  let resolve: ((value: api.Session) => void) | undefined
  vi.mocked(api.getSession).mockImplementation(() => new Promise(done => { resolve = done }))
  vi.mocked(api.logout).mockResolvedValue()
  const auth = useAuthStore()
  const first = auth.restore()
  const second = auth.restore()
  expect(api.getSession).toHaveBeenCalledTimes(1)
  await auth.signOut()
  resolve?.(session)
  expect(await first).toBe(false)
  expect(await second).toBe(false)
  expect(auth.authenticated).toBe(false)
})
it('会话过期与网络故障分别处理', async () => {
  const auth = useAuthStore()
  vi.mocked(api.getSession).mockRejectedValue(new ApiError('失效', 401))
  expect(await auth.restore()).toBe(false)
  vi.mocked(api.getSession).mockRejectedValue(new ApiError('服务不可用', 503))
  await expect(auth.restore()).rejects.toThrow('服务不可用')
})
