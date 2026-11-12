import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseSkill, publishSkill } from './skills'

const raw = {
  id: 's1', key: 'policy_qa', name: '制度问答', description: '', visibility: 'private', status: 'draft', current_version: 2,
  owner_user_id: 'u1', is_owner: true, can_manage: true, activation: 'on_demand', tool_keys: ['get_my_run_stats'],
  knowledge_scope: { mode: 'selected', kb_ids: ['kb1'] }, token_budget: 4000, content_md: '# SOP',
  created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z',
}

afterEach(() => vi.unstubAllGlobals())
describe('技能接口', () => {
  it('解析版本与知识库范围', () => {
    const skill = parseSkill(raw)
    expect(skill.current_version).toBe(2)
    expect(skill.knowledge_scope.kb_ids).toEqual(['kb1'])
    expect(() => parseSkill({ ...raw, activation: 'always_on' })).toThrow('数据格式')
  })
  it('发布指定版本', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...raw, status: 'published' }), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    expect((await publishSkill('s1', 2)).status).toBe('published')
    expect(String(fetcher.mock.calls[0]?.[0])).toContain('/api/skills/s1/publish')
    expect(JSON.parse(fetcher.mock.calls[0]?.[1]?.body as string)).toEqual({ version: 2 })
  })
})
