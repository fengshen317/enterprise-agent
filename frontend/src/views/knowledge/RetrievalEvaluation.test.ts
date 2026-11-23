import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { retrievalTest, type KnowledgeBase, type RetrievalTestChunk } from '../../api/knowledge'
import RetrievalEvaluation from './RetrievalEvaluation.vue'
import { metrics, parseSuite, scoreCase } from './retrievalEvaluation'

vi.mock('../../api/knowledge', () => ({ retrievalTest: vi.fn() }))
const question = { query: '库位？', document: '仓位.xlsx', evidence: 'C-17' }
const manifest = { name: '仓位评测', version: '1.0', top_k: 5, threshold: 0.2,
  cases: [question, { ...question, query: '哪个库位？' }] }
const kb = { id: 'k1', name: '测试库', parser_settings: {}, parser_id: 'naive', embedding_model: 'model' } as KnowledgeBase
const chunk: RetrievalTestChunk = { content: '货架 Ｃ-17', document_id: 'a', document_name: question.document, similarity: 0.6, positions: [] }

afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

it('按同一片段的来源和预期文字评分，并使用实际截断位置', () => {
  expect(() => parseSuite(JSON.stringify({ ...manifest, cases: [question, question] }))).toThrow('重复问题')
  const chunks = [{ ...chunk, document_name: '另一张表' }, chunk]
  const good = scoreCase(question, chunks, 2)
  const miss = scoreCase(question, chunks, 1)
  expect(good.rank).toBe(2)
  expect(miss.rank).toBeNull()
  expect(metrics([good, miss])).toEqual({ hitRate: 0.5, mrr: 0.25 })
})

it.each([true, false])('真实操作入口可导入并运行题集，完整结果才能导出 %s', async (fails) => {
  vi.stubGlobal('URL', class extends URL {
    static override createObjectURL = vi.fn(() => 'blob:evaluation')
    static override revokeObjectURL = vi.fn()
  })
  try {
    vi.mocked(retrievalTest).mockResolvedValueOnce({ effective_top_k: 3, chunks: [chunk] })
    if (fails) vi.mocked(retrievalTest).mockRejectedValueOnce(new Error('服务未就绪'))
    else vi.mocked(retrievalTest).mockResolvedValueOnce({ effective_top_k: 3, chunks: [] })
    const wrapper = mount(RetrievalEvaluation, { props: { kb } })
    const input = wrapper.get('input')
    Object.defineProperty(input.element, 'files', { value: [{ size: 1024, text: async () => JSON.stringify(manifest) }] })
    await input.trigger('change')
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(retrievalTest).toHaveBeenCalledWith('k1', { query: question.query, top_k: 5, threshold: 0.2 })
    expect(wrapper.text()).toContain('上限 3 条')
    if (fails) {
      expect(wrapper.get('[role="alert"]').text()).toContain('第 2 题未完成')
      expect(wrapper.find('a[href="blob:evaluation"]').exists()).toBe(false)
    } else {
      expect(wrapper.get('[role="status"]').text()).toContain('50.0%')
      expect(wrapper.get('[role="status"]').text()).toContain('0.500')
      expect(wrapper.get('a[href="blob:evaluation"]').attributes('download')).toBe('knowledge-evaluation.json')
    }
    wrapper.unmount()
  } finally { vi.unstubAllGlobals() }
})
