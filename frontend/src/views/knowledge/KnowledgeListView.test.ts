import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import KnowledgeListView from './KnowledgeListView.vue'
import * as api from '../../api/knowledge'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../api/knowledge', () => ({
  listKnowledgeBases: vi.fn(),
  createKnowledgeBase: vi.fn(),
  deleteKnowledgeBase: vi.fn(),
}))

const base: api.KnowledgeBase = {
  id: 'kb-1', name: '制度库', description: '公司制度', visibility: 'workspace', status: 'ready', error_code: null,
  parser_settings: {
    chunk_token_size: 512, delimiters: ['\n'], overlapped_percent: 0.1, use_parent_child: false,
    parent_child_delimiter: '\n', pdf_parse_method: 'deepdoc', auto_keywords_top_n: null, auto_questions_top_n: null,
  },
  parser_id: 'general', embedding_model: '', document_count: 2, chunk_count: 8,
  created_at: '2026-11-11T00:00:00Z', updated_at: '2026-11-11T00:00:00Z',
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([base])
  vi.mocked(api.createKnowledgeBase).mockResolvedValue(base)
  vi.mocked(api.deleteKnowledgeBase).mockResolvedValue()
})

function setup() {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = { user: { id: 'u1', username: 'admin', display_name: '管理员' }, csrf_token: 'csrf', expires_at: '', workspaces: [{ id: 'w1', slug: 'default', name: '测试空间', status: 'active', role: 'admin' }] }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/knowledge', component: KnowledgeListView },
      { path: '/knowledge/:kbId', component: { template: '<div>详情</div>' } },
    ],
  })
  return { pinia, router }
}

it('展示知识库统计并进入详情', async () => {
  const { pinia, router } = setup()
  await router.push('/knowledge')
  const wrapper = mount(KnowledgeListView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('制度库')
  expect(wrapper.text()).toContain('Chunk')
  expect(wrapper.text()).toContain('8')
  expect(wrapper.get('.knowledge-list__row-actions a').attributes('href')).toBe('/knowledge/kb-1')
  wrapper.unmount()
})

it('创建后携带解析配置并跳转详情', async () => {
  const { pinia, router } = setup()
  await router.push('/knowledge')
  const wrapper = mount(KnowledgeListView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  await wrapper.get('.knowledge-list__heading .knowledge-list__primary').trigger('click')
  await wrapper.get('[name="knowledgeName"]').setValue('新制度库')
  await wrapper.get('form.knowledge-list__form').trigger('submit')
  await flushPromises()
  expect(api.createKnowledgeBase).toHaveBeenCalledWith(expect.objectContaining({
    name: '新制度库',
    visibility: 'workspace',
    parser_id: 'general',
    parser_settings: expect.objectContaining({ chunk_token_size: 512, pdf_parse_method: 'deepdoc' }),
  }))
  expect(router.currentRoute.value.path).toBe('/knowledge/kb-1')
  wrapper.unmount()
})

it('删除需要二次确认', async () => {
  const { pinia, router } = setup()
  await router.push('/knowledge')
  const wrapper = mount(KnowledgeListView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  const button = wrapper.findAll('.knowledge-list__row-actions button')[0]!
  await button.trigger('click')
  expect(api.deleteKnowledgeBase).not.toHaveBeenCalled()
  await button.trigger('click')
  await flushPromises()
  expect(api.deleteKnowledgeBase).toHaveBeenCalledWith('kb-1')
  wrapper.unmount()
})
