import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import CapabilityCenterView from './CapabilityCenterView.vue'
import ConnectorConfigDialog from '../connectors/ConnectorConfigDialog.vue'
import * as skillApi from '../../api/skills'
import * as toolApi from '../../api/tools'
import { useAuthStore } from '../../stores/auth'
import { legacyConnectorsRoute, legacyPluginsRoute } from './capabilityRoutes'
import * as connectorApi from '../../api/connectors'

vi.mock('../../api/tools', () => ({ listTools: vi.fn(), setToolEnabled: vi.fn() }))
vi.mock('../../api/skills', () => ({ listSkills: vi.fn() }))
vi.mock('../../api/connectors', async original => ({
  ...await original<typeof connectorApi>(), listConnectors: vi.fn(), listBuiltinConnectors: vi.fn(),
}))

const tool: toolApi.Tool = {
  key: 'get_my_run_stats', version: 1, display_name: '我的运行统计', description: '统计运行', risk: 'read',
  approval: 'always', timeout_seconds: 5, quota_per_run: 4, input_schema: { properties: {} }, enabled: true, used_by_agents: 2,
}
const connectorTool: toolApi.Tool = { ...tool, key: 'search_external', display_name: '外部检索', connector_key: 'test' }
const connection: connectorApi.ConnectorView = {
  key: 'test', name: '测试连接器', description: '提供外部检索', kind: 'api', icon: '', enabled: true,
  status: 'connected', tool_count: 1, last_error: null, auth_status: 'not_required',
  last_error_code: null, last_success_at: null, last_checked_at: null, auth_action: null,
}
const skill: skillApi.Skill = {
  id: 's1', key: 'policy', name: '制度问答', description: '', visibility: 'workspace', status: 'published', current_version: 1,
  owner_user_id: 'u1', is_owner: false, can_manage: false, activation: 'always', tool_keys: ['get_my_run_stats'],
  knowledge_scope: { mode: 'all_readable', kb_ids: [] }, token_budget: 4000, content_md: null,
  created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z',
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(toolApi.listTools).mockResolvedValue([tool])
  vi.mocked(toolApi.setToolEnabled).mockResolvedValue({ ...tool, enabled: false })
  vi.mocked(skillApi.listSkills).mockResolvedValue([skill])
  vi.mocked(connectorApi.listConnectors).mockResolvedValue([connection])
  vi.mocked(connectorApi.listBuiltinConnectors).mockResolvedValue([])
})

function setup(role: 'admin' | 'member' = 'admin') {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = { user: { id: 'u1', username: 'tester', display_name: '测试用户' }, csrf_token: 'csrf', expires_at: '', workspaces: [{ id: 'w1', slug: 'default', name: '测试空间', status: 'active', role }] }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/capabilities', component: CapabilityCenterView },
      { path: '/admin/plugins', component: CapabilityCenterView },
      legacyConnectorsRoute,
      legacyPluginsRoute,
      { path: '/capabilities/skills/new', component: { template: '<div>新建技能</div>' } },
      { path: '/capabilities/skills/:skillId', component: { template: '<div>技能</div>' } },
    ],
  })
  return { pinia, router }
}

it('工具目录展示风险与审批，管理员可停用', async () => {
  const { pinia, router } = setup()
  await router.push('/capabilities')
  const wrapper = mount(CapabilityCenterView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('我的运行统计')
  expect(wrapper.text()).toContain('逐次审批')
  await wrapper.findAll('tbody button')[0]?.trigger('click')
  await flushPromises()
  expect(toolApi.setToolEnabled).toHaveBeenCalledWith('get_my_run_stats', false)
  wrapper.unmount()
})

it('技能库 Tab 展示版本与状态', async () => {
  const { pinia, router } = setup()
  await router.push('/capabilities')
  const wrapper = mount(CapabilityCenterView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  await wrapper.findAll('.capabilities__tabs button')[1]?.trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query.tab).toBe('skills')
  expect(wrapper.text()).toContain('制度问答')
  expect(wrapper.text()).toContain('已发布')
  wrapper.unmount()
})

it('旧连接器地址保留深链接参数，连接器页不请求工具或技能目录', async () => {
  const { pinia, router } = setup()
  await router.push('/connectors?connector=test&tab=skills')
  const wrapper = mount(RouterView, { global: { plugins: [pinia, router], stubs: { ConnectorConfigDialog: true } } })
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/capabilities')
  expect(router.currentRoute.value.query).toMatchObject({ tab: 'connectors', connector: 'test' })
  expect(wrapper.get('h1').text()).toBe('能力中心')
  expect(wrapper.text()).toContain('测试连接器')
  expect(wrapper.getComponent(ConnectorConfigDialog).props('connectorKey')).toBe('test')
  expect(toolApi.listTools).not.toHaveBeenCalled()
  expect(skillApi.listSkills).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('连接器可筛选它提供的工具，工具来源可返回连接器配置', async () => {
  vi.mocked(toolApi.listTools).mockResolvedValue([tool, connectorTool])
  const { pinia, router } = setup()
  await router.push('/capabilities?tab=connectors')
  const wrapper = mount(RouterView, { global: { plugins: [pinia, router], stubs: { ConnectorConfigDialog: true } } })
  await flushPromises()
  await wrapper.get('.connectors__tools-link').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query).toMatchObject({ tab: 'tools', source: 'test' })
  expect(wrapper.findAll('tbody tr')).toHaveLength(1)
  expect(wrapper.get('tbody').text()).toContain('外部检索')
  expect(wrapper.get('tbody').text()).toContain('连接器')
  await wrapper.get('tbody a').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query).toMatchObject({ tab: 'connectors', connector: 'test' })
  expect(wrapper.find('connector-config-dialog-stub').exists()).toBe(true)
  wrapper.unmount()
})

it('管理员可在能力中心切换插件管理，旧插件目录保留详情深链接', async () => {
  const { pinia, router } = setup()
  await router.push('/plugins?connector=test#catalog')
  const wrapper = mount(RouterView, { global: { plugins: [pinia, router], stubs: { ConnectorConfigDialog: true } } })
  await flushPromises()
  expect(router.currentRoute.value.fullPath).toBe('/capabilities?connector=test&tab=connectors#catalog')
  const nav = wrapper.get('[aria-label="能力分类"]')
  expect(nav.get('[aria-current="page"]').text()).toBe('插件')
  await nav.findAll('button').find(button => button.text() === '插件管理')!.trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/admin/plugins')
  expect(nav.get('[aria-current="page"]').text()).toBe('插件管理')
  expect(wrapper.findAll('h1').map(heading => heading.text())).toEqual(['能力中心'])
  expect(wrapper.text()).toContain('启停范围为全部工作空间')
  expect(toolApi.listTools).not.toHaveBeenCalled()
  await nav.findAll('button').find(button => button.text() === '插件')!.trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.fullPath).toBe('/capabilities?tab=connectors')
  wrapper.unmount()
})

it('普通成员只看到工具、技能和插件入口', async () => {
  const { pinia, router } = setup('member')
  await router.push('/capabilities?tab=management')
  const wrapper = mount(RouterView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  expect(wrapper.findAll('.capabilities__tabs button').map(button => button.text())).toEqual(['工具', '技能', '插件'])
  expect(wrapper.find('[aria-label="插件管理"]').exists()).toBe(false)
  wrapper.unmount()
})
