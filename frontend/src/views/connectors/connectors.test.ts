import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../../api/connectors'
import { useAuthStore } from '../../stores/auth'
import { useConnectorsStore } from '../../stores/connectors'
import ConnectorNotice from '../../components/ConnectorNotice.vue'
import ConnectorConfigDialog from './ConnectorConfigDialog.vue'
import ConnectorsView from './ConnectorsView.vue'

vi.mock('../../api/connectors', async original => ({
  ...await original<typeof api>(), listConnectors: vi.fn(), getConnector: vi.fn(),
  authenticateConnector: vi.fn(), getConnectorAuthStatus: vi.fn(), testConnector: vi.fn(),
  listBuiltinConnectors: vi.fn(),
}))

const health: api.ConnectorHealth = {
  auth_status: 'required', auth_action: 'authorize', last_error_code: 'auth_required',
  last_checked_at: '2026-11-18T00:00:00Z', last_success_at: null,
}
const connector: api.ConnectorView = {
  key: 'test', name: '测试连接器', kind: 'cli', description: '', icon: '', enabled: true,
  status: 'degraded', tool_count: 0, last_error: '需要登录', ...health,
}
const detail: api.ConnectorDetail = { ...connector, config_fields: [], config_values: {}, tools: [] }
const pending: api.AuthResult = {
  auth_url: 'https://example.com/authorize', output: '等待授权', already_logged_in: false,
  state: 'awaiting_user', expires_at: '2099-01-01T00:00:00Z',
}
function signIn(role: 'admin' | 'member' = 'admin'): void {
  const auth = useAuthStore()
  auth.session = {
    user: { id: 'u1', username: 'test', display_name: '测试' }, csrf_token: '', expires_at: '',
    workspaces: [
      { id: 'w1', name: '空间一', slug: 'one', status: 'active', role },
      { id: 'w2', name: '空间二', slug: 'two', status: 'active', role },
    ],
  }
  auth.selectedWorkspaceId = 'w1'
}
beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  vi.useFakeTimers()
  signIn()
  vi.mocked(api.listConnectors).mockResolvedValue([connector])
  vi.mocked(api.listBuiltinConnectors).mockResolvedValue([])
  vi.mocked(api.getConnector).mockResolvedValue(detail)
  vi.mocked(api.authenticateConnector).mockResolvedValue(pending)
  vi.mocked(api.getConnectorAuthStatus).mockResolvedValue(pending)
  vi.mocked(api.testConnector).mockResolvedValue({ ...health, status: 'connected', tool_count: 1, message: '健康' })
})
afterEach(() => { vi.useRealTimers() })

it('全局持续展示已启用连接器提醒，管理员入口携带连接器参数', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }, { path: '/connectors', component: { template: '<div />' } }] })
  const wrapper = mount(ConnectorNotice, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('测试连接器需要授权')
  expect(wrapper.get('a').attributes('href')).toBe('/connectors?connector=test')
  useConnectorsStore().connectors = [{ ...connector, enabled: false }, { ...connector, key: 'config', auth_action: 'configure', auth_status: 'unknown' }]
  await flushPromises()
  expect(wrapper.text()).toContain('需要配置')
  expect(wrapper.findAll('a')).toHaveLength(1)
  wrapper.unmount()
})

it('成员只看到联系管理员，抽屉不提供写操作', async () => {
  signIn('member')
  const notice = mount(ConnectorNotice, { global: { stubs: { RouterLink: true } } })
  const drawer = mount(ConnectorConfigDialog, { props: { connectorKey: 'test' } })
  await flushPromises()
  expect(notice.text()).toContain('请联系管理员处理')
  expect(notice.find('a').exists()).toBe(false)
  expect(drawer.find('footer').exists()).toBe(false)
  expect(api.authenticateConnector).not.toHaveBeenCalled()
  notice.unmount()
  drawer.unmount()
})

it('连接器页通过提醒参数打开对应抽屉并显示健康时间', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/connectors', component: ConnectorsView }] })
  await router.push('/connectors?connector=test')
  const wrapper = mount(ConnectorsView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.find('.connector-dialog').exists()).toBe(true)
  expect(api.getConnector).toHaveBeenCalledWith('test')
  expect(wrapper.get('.connectors__card').text()).toContain('需要授权')
  expect(wrapper.get('.connectors__card').text()).toContain('最近成功 尚无记录')
  expect(api.authenticateConnector).not.toHaveBeenCalled()
  await wrapper.get('.connector-dialog__close').trigger('click')
  await flushPromises()
  expect(wrapper.find('.connector-dialog').exists()).toBe(false)
  expect(router.currentRoute.value.query.connector).toBeUndefined()
  wrapper.unmount()
})

it('列表刷新共享请求，慢请求完成后间隔十五秒继续', async () => {
  let resolve: ((value: api.ConnectorView[]) => void) | undefined
  vi.mocked(api.listConnectors).mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const store = useConnectorsStore()
  store.acquire()
  store.acquire()
  void store.refresh()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(api.listConnectors).toHaveBeenCalledTimes(1)
  resolve?.([connector])
  await flushPromises()
  await vi.advanceTimersByTimeAsync(14_999)
  expect(api.listConnectors).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(api.listConnectors).toHaveBeenCalledTimes(2)
  store.release()
  store.release()
})

it('空间切换清空旧数据并忽略旧列表响应', async () => {
  let resolve: ((value: api.ConnectorView[]) => void) | undefined
  vi.mocked(api.listConnectors).mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const store = useConnectorsStore()
  store.acquire()
  useAuthStore().selectWorkspace('w2')
  await flushPromises()
  expect(store.connectors).toEqual([connector])
  resolve?.([{ ...connector, name: '旧空间' }])
  await flushPromises()
  expect(store.connectors[0]?.name).toBe('测试连接器')
  store.release()
})

it('页面隐藏时暂停刷新，恢复可见时立即更新', async () => {
  const store = useConnectorsStore()
  store.acquire()
  await flushPromises()
  const visibility = vi.spyOn(document, 'visibilityState', 'get')
  visibility.mockReturnValue('hidden')
  document.dispatchEvent(new Event('visibilitychange'))
  await vi.advanceTimersByTimeAsync(60_000)
  expect(api.listConnectors).toHaveBeenCalledTimes(1)
  visibility.mockReturnValue('visible')
  document.dispatchEvent(new Event('visibilitychange'))
  await flushPromises()
  expect(api.listConnectors).toHaveBeenCalledTimes(2)
  store.release()
  visibility.mockRestore()
})

it('授权使用安全链接，串行轮询完成后自动检查并清除提醒', async () => {
  let resolve: ((value: api.AuthResult) => void) | undefined
  vi.mocked(api.getConnectorAuthStatus).mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const store = useConnectorsStore()
  await store.refresh()
  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'test' } })
  await flushPromises()
  await wrapper.get('footer button').trigger('click')
  await flushPromises()
  expect(wrapper.get('a').attributes('href')).toBe(pending.auth_url)
  expect(wrapper.get('a').attributes('rel')).toContain('noopener')
  await vi.advanceTimersByTimeAsync(10_000)
  expect(api.getConnectorAuthStatus).toHaveBeenCalledTimes(1)
  vi.mocked(api.listConnectors).mockResolvedValue([{ ...connector, status: 'connected', auth_status: 'authorized', auth_action: null }])
  resolve?.({ ...pending, state: 'completed' })
  await flushPromises()
  expect(api.testConnector).toHaveBeenCalledWith('test')
  expect(store.attention).toEqual([])
  expect(wrapper.text()).toContain('授权已完成')
  await vi.advanceTimersByTimeAsync(10_000)
  expect(api.getConnectorAuthStatus).toHaveBeenCalledTimes(1)
  wrapper.unmount()
})

it('授权到期停止轮询并允许重新发起', async () => {
  vi.mocked(api.authenticateConnector).mockResolvedValue({ ...pending, expires_at: new Date(Date.now() + 3000).toISOString() })
  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'test' } })
  await flushPromises()
  await wrapper.get('footer button').trigger('click')
  await flushPromises()
  await vi.advanceTimersByTimeAsync(8000)
  expect(api.getConnectorAuthStatus).toHaveBeenCalledTimes(1)
  expect(wrapper.text()).toContain('授权已过期')
  wrapper.unmount()
})

it('切换空间后迟到的授权完成响应无法触发检查', async () => {
  let resolve: ((value: api.AuthResult) => void) | undefined
  vi.mocked(api.getConnectorAuthStatus).mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'test' } })
  await flushPromises()
  await wrapper.get('footer button').trigger('click')
  await flushPromises()
  await vi.advanceTimersByTimeAsync(2000)
  useAuthStore().selectWorkspace('w2')
  resolve?.({ ...pending, state: 'completed' })
  await flushPromises()
  expect(api.testConnector).not.toHaveBeenCalled()
  expect(wrapper.find('a').exists()).toBe(false)
  wrapper.unmount()
})

it('卸载停止旧授权轮询，拒绝非HTTPS授权地址', async () => {
  vi.mocked(api.authenticateConnector).mockResolvedValue({ ...pending, auth_url: 'javascript:alert(1)' })
  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'test' } })
  await flushPromises()
  await wrapper.get('footer button').trigger('click')
  await flushPromises()
  expect(wrapper.find('a').exists()).toBe(false)
  wrapper.unmount()
  await vi.advanceTimersByTimeAsync(10_000)
  expect(api.getConnectorAuthStatus).not.toHaveBeenCalled()
})

it('健康标签区分未启用、授权、配置、异常和待确认', () => {
  expect(api.connectorHealthLabel({ ...connector, enabled: false })).toBe('未启用')
  expect(api.connectorHealthLabel(connector)).toBe('需要授权')
  expect(api.connectorHealthLabel({ ...connector, auth_status: 'unknown', auth_action: 'configure' })).toBe('需要配置')
  expect(api.connectorHealthLabel({ ...connector, auth_status: 'required', auth_action: 'configure' })).toBe('需要配置')
  expect(api.connectorHealthLabel({ ...connector, auth_status: 'authorized', auth_action: 'retry' })).toBe('连接异常')
  expect(api.connectorHealthLabel({ ...connector, status: 'connected', auth_status: 'unknown', auth_action: null })).toBe('待确认')
  expect(api.connectorHealthLabel({ ...connector, status: 'connected', auth_status: 'authorized', auth_action: null })).toBe('健康')
  expect(api.connectorHealthLabel({ ...connector, auth_status: 'unknown', auth_action: 'retry', last_error_code: 'timeout' })).toBe('待确认')
  expect(api.connectorHealthLabel({ ...connector, auth_status: 'unknown', auth_action: 'retry', last_error_code: 'check_failed' })).toBe('待确认')
  expect(api.connectorHealthLabel({ ...connector, auth_status: 'unknown', auth_action: 'retry', last_error_code: null })).toBe('待确认')
  expect(api.connectorHealthLabel({ ...connector, auth_status: 'unknown', auth_action: 'retry', last_error_code: 'network_error' })).toBe('连接异常')
})
