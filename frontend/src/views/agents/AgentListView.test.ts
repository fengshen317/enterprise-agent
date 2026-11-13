import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import AgentListView from './AgentListView.vue'
import * as api from '../../api/agents'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../api/agents', () => ({ listAgents: vi.fn(), archiveAgent: vi.fn() }))

const agent: api.Agent = {
  id: 'a1', name: '制度助手', description: '制度问答', icon: '◎', visibility: 'workspace', status: 'active', version: 2,
  model: 'deepseek-flash', owner_user_id: 'u1', is_owner: false, can_manage: false, instructions: null,
  tools: [{ key: 'get_my_run_stats', display_name: '统计', risk: 'read', approval: 'never', enabled: true }],
  skills: [], knowledge_scope: { mode: 'all_readable', kb_ids: [] }, allow_planning: true, allow_delegation: true,
  starter_prompts: [], created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z',
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listAgents).mockResolvedValue([agent])
  vi.mocked(api.archiveAgent).mockResolvedValue({ ...agent, status: 'archived' })
})

function setup() {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = { user: { id: 'u1', username: 'admin', display_name: '管理员' }, csrf_token: 'csrf', expires_at: '', workspaces: [{ id: 'w1', slug: 'default', name: '测试空间', status: 'active', role: 'admin' }] }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agents', component: AgentListView },
      { path: '/agents/new', component: { template: '<div>新建</div>' } },
      { path: '/agents/:agentId', component: { template: '<div>编辑</div>' } },
    ],
  })
  return { pinia, router }
}

it('展示 Agent 能力并在新建时进入编辑器', async () => {
  const { pinia, router } = setup()
  await router.push('/agents')
  const wrapper = mount(AgentListView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('制度助手')
  expect(wrapper.text()).toContain('1 个工具')
  expect(wrapper.text()).toContain('空间共享')
  await wrapper.get('.agents__primary').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/agents/new')
  wrapper.unmount()
})
