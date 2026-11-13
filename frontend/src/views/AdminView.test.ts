import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import AdminView from './AdminView.vue'
import * as api from '../api/admin'
import { useAuthStore } from '../stores/auth'
import { ApiError } from '../api/client'

vi.mock('../api/admin', () => ({ listMembers: vi.fn(), listAudits: vi.fn(), createMember: vi.fn(), updateMember: vi.fn() }))
const owner: api.Member = { user_id: 'u1', username: 'admin', display_name: '管理员', user_status: 'active', role: 'admin', status: 'active', joined_at: '', updated_at: '' }
const alice: api.Member = { ...owner, user_id: 'u2', username: 'alice', display_name: '成员甲', role: 'member' }
beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  vi.mocked(api.listMembers).mockResolvedValue({ items: [owner, alice], total: 2, offset: 0, limit: 20 })
  vi.mocked(api.listAudits).mockResolvedValue([])
  vi.mocked(api.createMember).mockResolvedValue(alice)
  vi.mocked(api.updateMember).mockResolvedValue({ ...alice, role: 'admin' })
})
async function setup(role: api.Role = 'admin') {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = { user: { id: 'u1', username: 'admin', display_name: '管理员' }, csrf_token: 'csrf', expires_at: '', workspaces: [{ id: 'w1', slug: 'default', name: '测试空间', status: 'active', role }] }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/admin/members', component: AdminView }, { path: '/chat', component: { template: '<div>问答</div>' } }] })
  await router.push('/admin/members')
  const wrapper = mount(AdminView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper, auth, router }
}
it('普通成员不能读取管理列表或创建用户', async () => {
  const { wrapper } = await setup('member')
  expect(api.listMembers).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('仅当前工作空间的管理员')
  expect(wrapper.find('.admin__create').exists()).toBe(false)
  wrapper.unmount()
})
it('管理员创建用户，密码明文且不施加字符长度限制', async () => {
  const { wrapper } = await setup()
  expect(wrapper.text()).toContain('成员甲')
  await wrapper.get('.admin__heading button').trigger('click')
  await wrapper.get('[name="username"]').setValue('alice')
  await wrapper.get('[name="displayName"]').setValue('成员甲')
  const password = wrapper.get('[name="newPassword"]')
  expect(password.attributes('type')).toBe('text')
  expect(password.attributes('maxlength')).toBeUndefined()
  await password.setValue('1')
  await wrapper.get('.admin__create').trigger('submit')
  await flushPromises()
  expect(api.createMember).toHaveBeenCalledWith({ username: 'alice', display_name: '成员甲', password: '1', role: 'member' })
  expect(wrapper.text()).toContain('用户已创建')
  expect(wrapper.find('.admin__create').exists()).toBe(false)
  wrapper.unmount()
})
it('设置角色和停用状态后提交并刷新审计', async () => {
  const { wrapper } = await setup()
  await wrapper.get('select[aria-label="alice的角色"]').setValue('admin')
  await wrapper.get('select[aria-label="alice的空间访问"]').setValue('revoked')
  await wrapper.findAll('.admin__save')[1]!.trigger('click')
  await flushPromises()
  expect(api.updateMember).toHaveBeenCalledWith('u2', { role: 'admin', status: 'revoked' })
  expect(wrapper.text()).toContain('成员权限已更新')
  expect(api.listAudits).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})
it('最后管理员保护和重复账号错误可见，不显示成功', async () => {
  vi.mocked(api.updateMember).mockRejectedValue(new ApiError('请至少保留一名有效管理员', 409))
  const { wrapper } = await setup()
  await wrapper.get('select[aria-label="admin的角色"]').setValue('member')
  await wrapper.findAll('.admin__save')[0]!.trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('至少保留一名')
  expect(wrapper.text()).not.toContain('成员权限已更新')
  wrapper.unmount()
})
it('管理员自降级后重新获取身份并退出管理页面', async () => {
  const { wrapper, auth, router } = await setup()
  vi.spyOn(auth, 'restore').mockImplementation(async () => { if (auth.session) auth.session.workspaces[0]!.role = 'member'; return true })
  await wrapper.get('select[aria-label="admin的角色"]').setValue('member')
  await wrapper.findAll('.admin__save')[0]!.trigger('click')
  await flushPromises()
  expect(auth.restore).toHaveBeenCalled()
  expect(router.currentRoute.value.path).toBe('/chat')
  wrapper.unmount()
})
it('按账号搜索从第一页重新加载', async () => {
  const { wrapper } = await setup()
  await wrapper.get('#member-search').setValue('alice')
  await wrapper.get('.admin__toolbar').trigger('submit')
  await flushPromises()
  expect(api.listMembers).toHaveBeenLastCalledWith(0, 'alice')
  wrapper.unmount()
})
