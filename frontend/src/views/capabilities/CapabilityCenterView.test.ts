import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import CapabilityCenterView from './CapabilityCenterView.vue'
import * as skillApi from '../../api/skills'
import * as toolApi from '../../api/tools'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../api/tools', () => ({ listTools: vi.fn(), setToolEnabled: vi.fn() }))
vi.mock('../../api/skills', () => ({ listSkills: vi.fn() }))

const tool: toolApi.Tool = {
  key: 'get_my_run_stats', version: 1, display_name: '我的运行统计', description: '统计运行', risk: 'read',
  approval: 'always', timeout_seconds: 5, quota_per_run: 4, input_schema: { properties: {} }, enabled: true, used_by_agents: 2,
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
})

function setup() {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = { user: { id: 'u1', username: 'admin', display_name: '管理员' }, csrf_token: 'csrf', expires_at: '', workspaces: [{ id: 'w1', slug: 'default', name: '测试空间', status: 'active', role: 'admin' }] }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/capabilities', component: CapabilityCenterView },
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
