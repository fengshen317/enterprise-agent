import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import LoginView from './LoginView.vue'
import * as api from '../api/auth'
import { ApiError } from '../api/client'

vi.mock('../api/auth', () => ({ getSession: vi.fn(), login: vi.fn(), logout: vi.fn() }))
beforeEach(() => { vi.resetAllMocks(); localStorage.clear() })
it('提交登录后清除口令并进入工作台', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/login', component: LoginView }, { path: '/chat', component: { template: '<div>问答</div>' } }] })
  await router.push('/login')
  vi.mocked(api.login).mockResolvedValue({ user: { id: 'u1', username: 'alice', display_name: '甲' }, workspaces: [], csrf_token: 'csrf', expires_at: '' })
  const wrapper = mount(LoginView, { global: { plugins: [createPinia(), router] } })
  await wrapper.get('#username').setValue('alice')
  await wrapper.get('#password').setValue('temporary-password')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(api.login).toHaveBeenCalledWith('alice', 'temporary-password')
  expect(router.currentRoute.value.path).toBe('/chat')
  expect(wrapper.get<HTMLInputElement>('#password').element.value).toBe('')
  wrapper.unmount()
})
it('登录失败展示公开错误并清空密码', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/login', component: LoginView }] })
  await router.push('/login')
  vi.mocked(api.login).mockRejectedValue(new ApiError('账号或密码错误', 401, 'request-id'))
  const wrapper = mount(LoginView, { global: { plugins: [createPinia(), router] } })
  await wrapper.get('#username').setValue('alice')
  await wrapper.get('#password').setValue('wrong')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('账号或密码错误')
  expect(wrapper.get<HTMLInputElement>('#password').element.value).toBe('')
  expect(router.currentRoute.value.path).toBe('/login')
  wrapper.unmount()
})
