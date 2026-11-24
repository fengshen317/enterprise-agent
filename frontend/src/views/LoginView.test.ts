import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import LoginView from './LoginView.vue'
import * as api from '../api/auth'
import * as initialSetup from '../api/initialSetup'
import { ApiError } from '../api/client'

vi.mock('../api/auth', () => ({ getSession: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('../api/initialSetup', () => ({ getInitialSetup: vi.fn(), createInitialAdmin: vi.fn() }))
beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  vi.mocked(initialSetup.getInitialSetup).mockResolvedValue({ available: false, username: null })
})

it.each([['四字符密码', '短密码1'], ['长密码', 'a'.repeat(257)]])('部署表单接受%s', async (_, password) => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/login', component: LoginView }] })
  await router.push('/login')
  vi.mocked(initialSetup.getInitialSetup).mockResolvedValue({ available: true, username: null })
  vi.mocked(initialSetup.createInitialAdmin).mockResolvedValue('deploy-admin')
  const wrapper = mount(LoginView, { global: { plugins: [createPinia(), router] } })
  await flushPromises()
  const token = 'temporary-setup-token-for-test-only'
  await wrapper.get('#setup-token').setValue(token)
  await wrapper.get('#setup-username').setValue('deploy-admin')
  for (const selector of ['#setup-password', '#setup-confirm-password']) {
    const input = wrapper.get(selector)
    expect(input.attributes('minlength')).toBe('4')
    expect(input.attributes('maxlength')).toBeUndefined()
    expect(input.attributes('required')).toBeDefined()
  }
  for (const shortPassword of ['', '短密1', '🔑🔒']) {
    await wrapper.get('#setup-password').setValue(shortPassword)
    await wrapper.get('#setup-confirm-password').setValue(shortPassword)
    await wrapper.get('form').trigger('submit')
    expect(initialSetup.createInitialAdmin).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('至少需要 4 个字符')
  }
  await wrapper.get('#setup-password').setValue(password)
  await wrapper.get('#setup-confirm-password').setValue('mismatch')
  await wrapper.get('form').trigger('submit')
  expect(initialSetup.createInitialAdmin).not.toHaveBeenCalled()
  expect(wrapper.get('[role="alert"]').text()).toContain('密码不一致')
  await wrapper.get('#setup-confirm-password').setValue(password)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(initialSetup.createInitialAdmin).toHaveBeenCalledWith({
    token, username: 'deploy-admin', display_name: '管理员', password,
  })
  expect(wrapper.get('[role="status"]').text()).toContain('管理员已创建')
  expect(wrapper.get<HTMLInputElement>('#username').element.value).toBe('deploy-admin')
  expect(wrapper.get<HTMLInputElement>('#password').element.value).toBe('')
  expect(wrapper.find('#setup-token').exists()).toBe(false)
  wrapper.unmount()
})
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
