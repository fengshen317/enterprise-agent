import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { toDataURL } from 'qrcode'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../../api/connectors'
import { useAuthStore } from '../../stores/auth'
import { useConnectorsStore } from '../../stores/connectors'
import ConnectorNotice from '../../components/ConnectorNotice.vue'
import ConnectorConfigDialog from './ConnectorConfigDialog.vue'
import ConnectorsView from './ConnectorsView.vue'

vi.mock('qrcode', () => ({ toDataURL: vi.fn() }))
vi.mock('../../api/connectors', async original => ({
  ...await original<typeof api>(), listConnectors: vi.fn(), getConnector: vi.fn(),
  authenticateConnector: vi.fn(), getConnectorAuthStatus: vi.fn(), testConnector: vi.fn(),
  listBuiltinConnectors: vi.fn(), getYangJiBaoHoldings: vi.fn(),
  installConnector: vi.fn(), setConnectorEnabled: vi.fn(),
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

it('已保存的密钥显示配置状态，输入框保持空白', async () => {
  vi.mocked(api.getConnector).mockResolvedValue({
    ...detail,
    config_fields: [{
      key: 'api_key', field_type: 'secret', label: 'API Key', is_secret: true,
      required: true, default: '', placeholder: '',
    }],
    config_values: { api_key: 'tvly-***' },
  })
  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'tavily_search' } })
  await flushPromises()
  const input = wrapper.get<HTMLInputElement>('input[type="password"]')
  expect(input.element.value).toBe('')
  expect(input.attributes('placeholder')).toBe('已配置，留空保留原密钥')
  wrapper.unmount()
})

it('连接器页通过提醒参数打开对应抽屉并显示健康时间', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/connectors', component: ConnectorsView }] })
  await router.push('/connectors?connector=test')
  const wrapper = mount(ConnectorsView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.find('.connector-dialog').exists()).toBe(true)
  expect(api.getConnector).toHaveBeenCalledWith('test')
  expect(wrapper.get('.connectors__card .connectors__action--add').attributes('aria-label')).toBe('连接测试连接器')
  expect(wrapper.get('.connector-dialog').text()).toContain('最近成功 尚无记录')
  expect(api.authenticateConnector).not.toHaveBeenCalled()
  await wrapper.get('.connector-dialog__close').trigger('click')
  await flushPromises()
  expect(wrapper.find('.connector-dialog').exists()).toBe(false)
  expect(router.currentRoute.value.query.connector).toBeUndefined()
  wrapper.unmount()
})

it('同一网格展示连接器并通过加号安装和启用', async () => {
  const healthy: api.ConnectorView = {
    ...connector, status: 'connected', auth_status: 'authorized', auth_action: null, last_error_code: null,
  }
  const candidate: api.BuiltinConnector = {
    key: 'fund_market', kind: 'api', name: '基金行情', description: '查看估值和净值', icon: '📈',
    installed: false, config_fields: [],
  }
  const added: api.ConnectorView = { ...healthy, ...candidate, auth_status: 'not_required' }
  vi.mocked(api.listBuiltinConnectors).mockResolvedValue([
    { ...candidate, key: healthy.key, name: healthy.name, installed: true }, candidate,
  ])
  vi.mocked(api.listConnectors).mockResolvedValueOnce([healthy]).mockResolvedValue([healthy, added])
  vi.mocked(api.installConnector).mockResolvedValue({ ...detail, ...added, enabled: false })
  vi.mocked(api.setConnectorEnabled).mockResolvedValue({ ...detail, ...added })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/connectors', component: ConnectorsView }] })
  await router.push('/connectors')
  const wrapper = mount(ConnectorsView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.findAll('.connectors__grid')).toHaveLength(1)
  expect(wrapper.findAll('.connectors__card')).toHaveLength(2)
  expect(wrapper.get('[data-connector="test"] [role="img"]').attributes('aria-label')).toBe('健康')
  expect(wrapper.get('[data-connector="fund_market"] button').text()).toBe('+')
  await wrapper.get('input[type="search"]').setValue('基金')
  expect(wrapper.findAll('.connectors__card')).toHaveLength(1)
  await wrapper.get('[data-connector="fund_market"] button').trigger('click')
  await flushPromises()
  expect(api.installConnector).toHaveBeenCalledExactlyOnceWith('fund_market')
  expect(api.setConnectorEnabled).toHaveBeenCalledExactlyOnceWith('fund_market', true)
  expect(wrapper.find('button[aria-label="管理基金行情"]').exists()).toBe(true)
  wrapper.unmount()
})

it('已安装但停用的连接器可以在抽屉启用和停用', async () => {
  const disabled: api.ConnectorDetail = { ...detail, status: 'disabled', enabled: false }
  vi.mocked(api.setConnectorEnabled).mockResolvedValue(disabled)
  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'test' } })
  await flushPromises()
  vi.mocked(api.getConnector).mockResolvedValue(disabled)
  const toggle = wrapper.get('.connector-dialog__section button')
  expect(toggle.text()).toBe('停用连接器')
  await toggle.trigger('click')
  await flushPromises()
  expect(api.setConnectorEnabled).toHaveBeenCalledExactlyOnceWith('test', false)
  expect(toggle.text()).toBe('启用连接器')
  wrapper.unmount()
})

it('绑定账号可在抽屉同步持仓，单独展示两类涨跌幅和对应时间', async () => {
  vi.mocked(api.getConnector).mockResolvedValue({ ...detail, key: 'yangjibao', can_view_holdings: true })
  vi.mocked(api.getYangJiBaoHoldings).mockResolvedValue({
    synced_at: '2026-11-18T06:00:00Z', fund_count: 1,
    funds: [{
      code: '000001', name: '测试基金', holding_amount: 1234.56, estimated_change_percent: 0,
      latest_nav_change_percent: -1.25, quote_time: '2026-11-18 13:55', nav_date: '2026-11-17',
      share: 600.1234, cost_amount: 1050, estimated_today_profit_amount: -3.5,
    }],
  })
  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'yangjibao' } })
  await flushPromises()
  expect(api.getYangJiBaoHoldings).not.toHaveBeenCalled()
  await wrapper.get('.yangjibao-holdings button').trigger('click')
  await flushPromises()
  const table = wrapper.get('table').text()
  for (const text of ['持仓金额', '盘中估算涨跌幅', '披露净值涨跌幅', '1,234.56', '0.00%', '-1.25%', '13:55', '2026-11-17']) {
    expect(table).toContain(text)
  }
  const expand = wrapper.get('.yangjibao-holdings__expand')
  expect(expand.attributes('aria-expanded')).toBe('false')
  await expand.trigger('click')
  expect(expand.attributes('aria-expanded')).toBe('true')
  const details = wrapper.get('dl').text()
  for (const text of ['持有份额', '600.1234', '持仓成本', '1,050.00', '参考单日收益', '3.50', '暂无']) {
    expect(details).toContain(text)
  }
  await expand.trigger('click')
  expect(wrapper.find('dl').exists()).toBe(false)
  vi.mocked(api.getConnector).mockResolvedValue({ ...detail, key: 'yangjibao', can_view_holdings: false })
  useAuthStore().selectWorkspace('w2')
  await flushPromises()
  expect(wrapper.find('.yangjibao-holdings').exists()).toBe(false)
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

it('养基宝在抽屉内显示微信二维码，过期可重新获取且成功后刷新状态', async () => {
  const qr = {
    ...pending, auth_url: '', qr_content: 'http://weixin.qq.com/q/test',
    output: '请打开微信扫一扫，按手机提示确认授权。',
  }
  vi.mocked(api.getConnector).mockResolvedValue({ ...detail, key: 'yangjibao', name: '养基宝持仓' })
  vi.mocked(api.authenticateConnector).mockResolvedValue(qr)
  vi.mocked(api.getConnectorAuthStatus)
    .mockResolvedValueOnce({ ...qr, qr_content: '', state: 'expired' })
    .mockResolvedValueOnce({ ...qr, qr_content: '', state: 'completed' })
  vi.mocked(toDataURL).mockImplementation(async () => 'data:image/png;base64,test')

  const wrapper = mount(ConnectorConfigDialog, { props: { connectorKey: 'yangjibao' } })
  await flushPromises()
  const action = wrapper.get('footer button')
  expect(action.text()).toBe('扫码连接')
  expect(wrapper.text()).not.toContain('保存配置')
  await action.trigger('click')
  await flushPromises()
  expect(wrapper.get('img').attributes('alt')).toContain('微信扫码')
  expect(toDataURL).toHaveBeenCalledWith(qr.qr_content, expect.objectContaining({ width: 220 }))
  expect(wrapper.text()).toContain('等待微信扫码')
  expect(wrapper.find('a').exists()).toBe(false)

  await vi.advanceTimersByTimeAsync(2000)
  expect(wrapper.find('img').exists()).toBe(false)
  expect(action.text()).toBe('重新获取二维码')
  await action.trigger('click')
  await flushPromises()
  expect(wrapper.find('img').exists()).toBe(true)
  await vi.advanceTimersByTimeAsync(2000)
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.text()).toContain('授权已完成')
  expect(api.testConnector).toHaveBeenCalledWith('yangjibao')
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
