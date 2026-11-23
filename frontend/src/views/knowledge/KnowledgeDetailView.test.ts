import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import KnowledgeDetailView from './KnowledgeDetailView.vue'
import * as api from '../../api/knowledge'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../api/knowledge', () => ({
  getKnowledgeBase: vi.fn(),
  getKnowledgeDocumentReaders: vi.fn(),
  setKnowledgeDocumentReaders: vi.fn(),
  listKnowledgeAccessMembers: vi.fn(),
  maxDocumentReaders: 50,
  listKnowledgeDocuments: vi.fn(),
  updateKnowledgeBase: vi.fn(),
  reparseAllDocuments: vi.fn(),
  retrievalTest: vi.fn(),
  deleteKnowledgeDocument: vi.fn(),
  uploadKnowledgeDocument: vi.fn(),
  parseKnowledgeDocument: vi.fn(),
  documentFileUrl: vi.fn((id: string) => '/file/' + id),
}))

const base: api.KnowledgeBase = {
  id: 'kb-1', name: '制度库', description: '公司制度', visibility: 'workspace', status: 'ready', error_code: null, can_manage: true,
  parser_settings: {
    chunk_token_size: 512, delimiters: ['\n'], overlapped_percent: 0.1, use_parent_child: false,
    parent_child_delimiter: '\n', pdf_parse_method: 'deepdoc', auto_keywords_top_n: null, auto_questions_top_n: null,
  },
  parser_id: 'general', embedding_model: '', document_count: 1, chunk_count: 3,
  created_at: '2026-11-11T00:00:00Z', updated_at: '2026-11-11T00:00:00Z',
}
const document: api.KnowledgeDocument = {
  id: 'doc-1', kb_id: 'kb-1', name: '制度.txt', mime: 'text/plain', size: 1024, sha256: 'x', status: 'ready',
  error_code: null, progress: 1, error_message: null, chunk_count: 3, token_count: 100,
  created_at: '2026-11-11T00:00:00Z', updated_at: '2026-11-11T00:00:00Z',
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.getKnowledgeBase).mockResolvedValue(base)
  vi.mocked(api.listKnowledgeDocuments).mockResolvedValue([document])
  vi.mocked(api.updateKnowledgeBase).mockResolvedValue(base)
  vi.mocked(api.reparseAllDocuments).mockResolvedValue([document])
  vi.mocked(api.retrievalTest).mockResolvedValue({ effective_top_k: 5, chunks: [{ content: '命中内容', document_id: 'doc-1', document_name: '制度.txt', similarity: 0.9, positions: [1] }] })
  vi.mocked(api.deleteKnowledgeDocument).mockResolvedValue()
  vi.mocked(api.uploadKnowledgeDocument).mockResolvedValue(document)
  vi.mocked(api.parseKnowledgeDocument).mockResolvedValue(document)
})

async function setup() {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = { user: { id: 'u1', username: 'admin', display_name: '管理员' }, csrf_token: 'csrf', expires_at: '', workspaces: [{ id: 'w1', slug: 'default', name: '测试空间', status: 'active', role: 'admin' }] }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/knowledge', component: { template: '<div>列表</div>' } },
      { path: '/knowledge/:kbId', component: KnowledgeDetailView },
    ],
  })
  await router.push('/knowledge/kb-1')
  const wrapper = mount(KnowledgeDetailView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper, router }
}

it('加载详情与文档列表', async () => {
  const { wrapper } = await setup()
  expect(api.getKnowledgeBase).toHaveBeenCalledWith('kb-1')
  expect(wrapper.text()).toContain('制度库')
  expect(wrapper.text()).toContain('制度.txt')
  wrapper.unmount()
})

it('切换解析配置与检索测试 Tab', async () => {
  const { wrapper } = await setup()
  const tabs = wrapper.findAll('.knowledge-detail__tabs button')
  await tabs[1]!.trigger('click')
  await flushPromises()
  expect(wrapper.find('[name="parserId"]').exists()).toBe(true)
  expect(wrapper.text()).toContain('应用到已有文档')
  await wrapper.findAll('.knowledge-detail__tabs button')[2]!.trigger('click')
  await flushPromises()
  expect(wrapper.find('[name="retrievalQuery"]').exists()).toBe(true)
  wrapper.unmount()
})

it('检索测试展示命中来源和相似度', async () => {
  const { wrapper } = await setup()
  await wrapper.findAll('.knowledge-detail__tabs button')[2]!.trigger('click')
  await flushPromises()
  await wrapper.get('[name="retrievalQuery"]').setValue('年假')
  await wrapper.get('form.retrieval-tab__form').trigger('submit')
  await flushPromises()
  expect(api.retrievalTest).toHaveBeenCalledWith('kb-1', expect.objectContaining({ query: '年假' }))
  expect(wrapper.text()).toContain('命中内容')
  expect(wrapper.text()).toContain('90.0%')
  wrapper.unmount()
})


it('选择受限上传时按指定读者提交文件，并允许编辑受限文档名单', async () => {
  const restricted = { ...document, restricted: true }
  vi.mocked(api.listKnowledgeDocuments).mockResolvedValue([restricted])
  vi.mocked(api.listKnowledgeAccessMembers).mockResolvedValue([
    { id: 'u1', username: 'admin', display_name: '管理员' },
    { id: 'u2', username: 'reader', display_name: '读者' },
  ])
  vi.mocked(api.getKnowledgeDocumentReaders).mockResolvedValue(['u1', 'u2'])
  vi.mocked(api.setKnowledgeDocumentReaders).mockResolvedValue(['u1'])
  const { wrapper } = await setup()
  await wrapper.get('.documents-tab__upload select').setValue('restricted')
  const file = new File(['示例'], '底价.txt', { type: 'text/plain' })
  const input = wrapper.get('input[type="file"]')
  Object.defineProperty(input.element, 'files', { value: [file] })
  await input.trigger('change')
  await wrapper.get('.documents-tab__primary').trigger('click')
  await flushPromises()
  const dialog = wrapper.get('[aria-label="文档读取权限"]')
  await dialog.findAll('input[type="checkbox"]')[1]!.setValue(true)
  await dialog.get('.document-readers__primary').trigger('click')
  await flushPromises()
  expect(api.uploadKnowledgeDocument).toHaveBeenCalledWith('kb-1', file, ['u1', 'u2'])
  await wrapper.findAll('.documents-tab__row-actions button').find(button => button.text() === '读取权限')!.trigger('click')
  await flushPromises()
  const editor = wrapper.get('[aria-label="文档读取权限"]')
  await editor.findAll('input[type="checkbox"]')[1]!.setValue(false)
  await editor.get('.document-readers__primary').trigger('click')
  await flushPromises()
  expect(api.setKnowledgeDocumentReaders).toHaveBeenCalledWith('doc-1', ['u1'])
  wrapper.unmount()
})
