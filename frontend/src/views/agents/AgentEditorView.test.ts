import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import AgentEditorView from './AgentEditorView.vue'
import * as agentApi from '../../api/agents'
import * as knowledgeApi from '../../api/knowledge'
import * as skillApi from '../../api/skills'
import * as toolApi from '../../api/tools'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../api/agents', () => ({ createAgent: vi.fn(), getAgent: vi.fn(), updateAgent: vi.fn() }))
vi.mock('../../api/tools', () => ({ listTools: vi.fn() }))
vi.mock('../../api/skills', () => ({ listSkills: vi.fn() }))
vi.mock('../../api/knowledge', () => ({ listKnowledgeBases: vi.fn() }))

const agent: agentApi.Agent = {
  id: 'a1', name: '统计助手', description: '', icon: '◎', visibility: 'private', status: 'active', version: 1,
  model: 'deepseek-flash', owner_user_id: 'u1', is_owner: true, can_manage: true, instructions: '',
  tools: [], skills: [], knowledge_scope: { mode: 'all_readable', kb_ids: [] }, allow_planning: true,
  allow_delegation: true, starter_prompts: [], created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z',
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(toolApi.listTools).mockResolvedValue([])
  vi.mocked(skillApi.listSkills).mockResolvedValue([])
  vi.mocked(knowledgeApi.listKnowledgeBases).mockResolvedValue([])
  vi.mocked(agentApi.createAgent).mockResolvedValue(agent)
})

function setup() {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = { user: { id: 'u1', username: 'admin', display_name: '管理员' }, csrf_token: 'csrf', expires_at: '', workspaces: [{ id: 'w1', slug: 'default', name: '测试空间', status: 'active', role: 'admin' }] }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agents/new', component: AgentEditorView },
      { path: '/agents/:agentId', component: AgentEditorView },
      { path: '/agents', component: { template: '<div>列表</div>' } },
    ],
  })
  return { pinia, router }
}

it('新建 Agent 提交名称与默认能力配置', async () => {
  const { pinia, router } = setup()
  await router.push('/agents/new')
  const wrapper = mount(AgentEditorView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  const inputs = wrapper.findAll('.agent-editor__fields input')
  await inputs[0]?.setValue('统计助手')
  await wrapper.get('.agent-editor__primary').trigger('click')
  await flushPromises()
  expect(agentApi.createAgent).toHaveBeenCalledWith(expect.objectContaining({
    name: '统计助手', visibility: 'private', knowledge_mode: 'all_readable',
  }))
  wrapper.unmount()
})
