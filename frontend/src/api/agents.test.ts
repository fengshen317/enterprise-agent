import { afterEach, describe, expect, it, vi } from 'vitest'
import { createAgent, parseAgent, type AgentInput } from './agents'

const raw = {
  id: 'a1', name: '制度助手', description: '', icon: '◎', visibility: 'private', status: 'active', version: 2,
  model: 'deepseek-flash', owner_user_id: 'u1', is_owner: true, can_manage: true, instructions: '先检索',
  tools: [{ key: 'get_my_run_stats', display_name: '统计', risk: 'read', approval: 'never', enabled: true }],
  skills: [{ id: 's1', key: 'policy', name: '制度技能', version: 1, activation: 'always' }],
  knowledge_scope: { mode: 'selected', kb_ids: ['kb1'] }, allow_planning: true, allow_delegation: true,
  starter_prompts: ['查询统计'], created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z',
}

afterEach(() => vi.unstubAllGlobals())
describe('Agent 接口', () => {
  it('严格解析能力绑定与知识库范围', () => {
    const agent = parseAgent(raw)
    expect(agent.tools[0]?.key).toBe('get_my_run_stats')
    expect(agent.skills[0]?.activation).toBe('always')
    expect(agent.knowledge_scope.kb_ids).toEqual(['kb1'])
    expect(() => parseAgent({ ...raw, visibility: 'public' })).toThrow('数据格式')
  })
  it('创建时提交工具、技能与知识库绑定', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(raw), { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    const input: AgentInput = {
      name: '制度助手', description: '', icon: '◎', instructions: '', visibility: 'private', status: 'active', model: '',
      allow_planning: true, allow_delegation: true, tool_keys: ['get_my_run_stats'],
      skills: [{ id: 's1', activation: 'on_demand' }], knowledge_mode: 'selected', knowledge_base_ids: ['kb1'], starter_prompts: [],
    }
    expect((await createAgent(input)).id).toBe('a1')
    expect(JSON.parse(fetcher.mock.calls[0]?.[1]?.body as string)).toMatchObject({
      tool_keys: ['get_my_run_stats'], skills: [{ id: 's1', activation: 'on_demand' }], knowledge_mode: 'selected',
    })
  })
})
