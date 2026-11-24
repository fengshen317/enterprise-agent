import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { ApiError } from '../../api/client'
import * as api from '../../api/connectors'
import { useAuthStore } from '../../stores/auth'
import AdminPluginsView from './AdminPluginsView.vue'
import ConnectorConfigDialog from './ConnectorConfigDialog.vue'

vi.mock('../../api/connectors', async original => ({
  ...await original<typeof api>(),
  listBuiltinConnectors: vi.fn(),
  listConnectors: vi.fn(),
  installConnector: vi.fn(),
  setConnectorEnabled: vi.fn(),
  getConnector: vi.fn(),
}))

const mail: api.BuiltinConnector = {
  key: 'qq_mail', name: 'QQ 邮箱', kind: 'api', icon: '✉️',
  description: '搜索和阅读本人 QQ 收件箱', config_fields: [], installed: false,
}
const meeting: api.BuiltinConnector = {
  key: 'tmeet', name: '腾讯会议', kind: 'cli', icon: '📹',
  description: '查询会议与日程', config_fields: [], installed: true,
}

function view(builtin: api.BuiltinConnector, enabled: boolean): api.ConnectorView {
  return {
    ...builtin, enabled, status: enabled ? 'connected' : 'disabled', tool_count: 0,
    last_error: null, last_error_code: null, auth_status: 'unknown', auth_action: null,
    last_checked_at: null, last_success_at: null,
  }
}

function detail(builtin: api.BuiltinConnector, enabled: boolean): api.ConnectorDetail {
  return { ...view(builtin, enabled), config_fields: [], config_values: {}, tools: [] }
}

async function setup(role: 'admin' | 'member' = 'admin', path = '/admin/plugins') {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = {
    user: { id: 'u1', username: 'tester', display_name: '测试用户' }, csrf_token: '', expires_at: '',
    workspaces: [
      { id: 'w1', name: '空间一', slug: 'one', status: 'active', role },
      { id: 'w2', name: '空间二', slug: 'two', status: 'active', role: 'admin' },
    ],
  }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/admin/plugins', component: AdminPluginsView }],
  })
  await router.push(path)
  const wrapper = mount(AdminPluginsView, { attachTo: document.body, global: { plugins: [pinia, router] } })
  await flushPromises()
  return { auth, router, wrapper }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listBuiltinConnectors).mockResolvedValue([mail, meeting])
  vi.mocked(api.listConnectors).mockResolvedValue([view(meeting, true)])
  vi.mocked(api.getConnector).mockResolvedValue(detail(meeting, true))
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false } },
  })
})

it('只向当前空间的管理员提供插件管理与深链详情', async () => {
  const { wrapper } = await setup('member', '/admin/plugins?connector=tmeet')
  expect(wrapper.get('[role="alert"]').text()).toContain('仅当前工作空间的管理员')
  expect(wrapper.find('[role="table"]').exists()).toBe(false)
  expect(api.listBuiltinConnectors).not.toHaveBeenCalled()
  expect(api.getConnector).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('按状态和名称筛选，并从已安装插件的深链打开管理详情', async () => {
  const { wrapper, router } = await setup('admin', '/admin/plugins?connector=tmeet')
  expect(wrapper.text()).toContain('启停范围为全部工作空间')
  expect(wrapper.getComponent(ConnectorConfigDialog).props()).toMatchObject({ connectorKey: 'tmeet', adminMode: true })
  await wrapper.getComponent(ConnectorConfigDialog).vm.$emit('close')
  await flushPromises()
  expect(router.currentRoute.value.query.connector).toBeUndefined()
  expect(wrapper.findComponent(ConnectorConfigDialog).exists()).toBe(false)

  await wrapper.get('select').setValue('enabled')
  expect(wrapper.findAll('[data-plugin]').map(item => item.attributes('data-plugin'))).toEqual(['tmeet'])
  await wrapper.get('input[type="search"]').setValue('QQ')
  expect(wrapper.text()).toContain('没有匹配的插件')
  await wrapper.get('select').setValue('all')
  expect(wrapper.get('[data-plugin="qq_mail"]').text()).toContain('成员自行绑定')
  await wrapper.get('[aria-label="查看 QQ 邮箱 详情"]').trigger('click')
  expect(wrapper.get('dialog[open]').text()).toContain('全部工作空间')
  expect(wrapper.get('dialog').text()).toContain('账号凭证仅用于本人运行')
  expect(api.getConnector).toHaveBeenCalledExactlyOnceWith('tmeet')
  wrapper.unmount()
})

it('二步开启失败后保留确认框，重试只执行启用', async () => {
  vi.mocked(api.installConnector).mockResolvedValue(detail(mail, false))
  vi.mocked(api.setConnectorEnabled)
    .mockRejectedValueOnce(new ApiError('暂时无法启用', 503))
    .mockImplementationOnce(async () => {
      vi.mocked(api.listConnectors).mockResolvedValue([view(meeting, true), view(mail, true)])
      return detail(mail, true)
    })
  const { wrapper } = await setup()
  await wrapper.get('[aria-label="启用 QQ 邮箱"]').trigger('click')
  expect(wrapper.get('dialog[open]').text()).toContain('适用于全部工作空间')
  expect(api.installConnector).not.toHaveBeenCalled()
  const confirmButton = () => wrapper.get('dialog[open] .admin-plugins__button--primary')
  await confirmButton().trigger('click')
  await flushPromises()
  expect(wrapper.get('dialog[open] [role="alert"]').text()).toBe('暂时无法启用')
  expect(wrapper.get('[data-plugin="qq_mail"]').text()).toContain('未启用')
  await confirmButton().trigger('click')
  await flushPromises()
  expect(wrapper.find('dialog[open]').exists()).toBe(false)
  expect(api.setConnectorEnabled).toHaveBeenNthCalledWith(1, 'qq_mail', true)
  expect(api.setConnectorEnabled).toHaveBeenNthCalledWith(2, 'qq_mail', true)
  expect(api.installConnector).toHaveBeenCalledExactlyOnceWith('qq_mail')
  expect(wrapper.get('[data-plugin="qq_mail"]').text()).toContain('已启用')
  expect(wrapper.text()).toContain('QQ 邮箱 已启用')
  wrapper.unmount()
})

it('安装冲突可继续启用，停用明确告知会保留个人绑定', async () => {
  vi.mocked(api.installConnector).mockRejectedValue(new ApiError('插件已安装', 409))
  vi.mocked(api.setConnectorEnabled).mockImplementation(async (_key, enabled) => {
    vi.mocked(api.listConnectors).mockResolvedValue([view(mail, enabled)])
    return detail(mail, enabled)
  })
  const { wrapper } = await setup()
  await wrapper.get('[aria-label="启用 QQ 邮箱"]').trigger('click')
  await wrapper.get('dialog .admin-plugins__button--primary').trigger('click')
  await flushPromises()
  expect(wrapper.get('[data-plugin="qq_mail"]').text()).toContain('已启用')
  await wrapper.get('[aria-label="停用 QQ 邮箱"]').trigger('click')
  expect(wrapper.get('dialog[open]').text()).toContain('成员已保存的账号绑定会保留')
  await wrapper.get('dialog .admin-plugins__button--danger').trigger('click')
  await flushPromises()
  expect(api.setConnectorEnabled).toHaveBeenNthCalledWith(2, 'qq_mail', false)
  expect(wrapper.text()).toContain('QQ 邮箱 已停用')
  wrapper.unmount()
})

it('安装期间拒绝重复确认，切换空间后丢弃旧结果并跳过后续启用', async () => {
  let finishInstallation: (result: api.ConnectorDetail) => void = () => undefined
  vi.mocked(api.installConnector).mockReturnValue(new Promise(resolve => { finishInstallation = resolve }))
  const { auth, wrapper } = await setup()
  await wrapper.get('[aria-label="启用 QQ 邮箱"]').trigger('click')
  const confirmButton = wrapper.get('dialog .admin-plugins__button--primary')
  const click = confirmButton.trigger('click')
  await confirmButton.trigger('click')
  await click
  expect(api.installConnector).toHaveBeenCalledExactlyOnceWith('qq_mail')

  auth.selectWorkspace('w2')
  finishInstallation(detail(mail, false))
  await flushPromises()
  expect(wrapper.find('dialog[open]').exists()).toBe(false)
  expect(api.setConnectorEnabled).not.toHaveBeenCalled()
  expect(api.listBuiltinConnectors).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})
