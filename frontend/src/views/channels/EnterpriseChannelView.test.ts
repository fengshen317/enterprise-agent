import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import EnterpriseChannelView from './EnterpriseChannelView.vue'
import * as api from '../../api/channels'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../api/channels', () => ({
  listEnterpriseApps: vi.fn(),
  loadEnterpriseBinding: vi.fn(),
  saveEnterpriseApp: vi.fn(),
  createBindCode: vi.fn(),
  unbindEnterprise: vi.fn(),
}))

const app = {
  channel_type: 'feishu' as const,
  external_app_id: 'cli_test',
  status: 'active',
  secret_configured: true,
  robot_code: '',
  sandbox: false,
  updated_at: null,
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listEnterpriseApps).mockResolvedValue([app])
  vi.mocked(api.loadEnterpriseBinding).mockResolvedValue({
    bound: false, account_id: null, platform_user_id: '', status: '', app_configured: true,
  })
  vi.mocked(api.saveEnterpriseApp).mockResolvedValue(app)
  vi.mocked(api.createBindCode).mockResolvedValue({
    code: 'ABCD2345', expires_at: '2099-01-01T00:00:00Z', instructions: '发送 /bind ABCD2345',
  })
})

function mountView(role: 'admin' | 'member' = 'admin') {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = {
    user: { id: 'u1', username: 'a', display_name: '甲' },
    csrf_token: 'csrf',
    expires_at: '',
    workspaces: [{ id: 'w1', slug: 'default', name: '空间', status: 'active', role }],
  }
  auth.selectedWorkspaceId = 'w1'
  return mount(EnterpriseChannelView, { props: { channel: 'feishu' }, global: { plugins: [pinia] } })
}

it('管理员可以保存飞书应用，员工可以生成绑定码', async () => {
  const wrapper = mountView()
  await flushPromises()
  expect(wrapper.text()).toContain('没有 24 小时')
  expect(wrapper.text()).toContain('密钥已保存')
  await wrapper.get('[name="externalAppId"]').setValue('cli_new')
  await wrapper.get('[name="appSecret"]').setValue('secret-value')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(api.saveEnterpriseApp).toHaveBeenCalledWith({
    channel_type: 'feishu', external_app_id: 'cli_new', secret: 'secret-value', robot_code: '', sandbox: false,
  })
  await wrapper.get('.enterprise__actions button').trigger('click')
  await flushPromises()
  expect(api.createBindCode).toHaveBeenCalledWith('feishu')
  expect(wrapper.text()).toContain('ABCD2345')
  wrapper.unmount()
})

it('普通成员看不到密钥表单', async () => {
  const wrapper = mountView('member')
  await flushPromises()
  expect(wrapper.find('form').exists()).toBe(false)
  expect(wrapper.text()).toContain('生成绑定码')
  wrapper.unmount()
})
