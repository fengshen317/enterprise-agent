import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createKnowledgeBase,
  documentFileUrl,
  getKnowledgeBase,
  listDocumentChunks,
  listKnowledgeBases,
  parseKnowledgeDocument,
  reparseAllDocuments,
  retrievalTest,
  uploadKnowledgeDocument,
  type KnowledgeBase,
} from './knowledge'
import { setSecurityContext } from './client'

const base: KnowledgeBase = {
  id: 'kb-1',
  name: '制度库',
  description: '公司制度',
  visibility: 'workspace',
  status: 'ready',
  error_code: null,
  parser_settings: {
    chunk_token_size: 512,
    delimiters: ['\n'],
    overlapped_percent: 0,
    use_parent_child: false,
    parent_child_delimiter: '\n',
    pdf_parse_method: 'deepdoc',
    auto_keywords_top_n: null,
    auto_questions_top_n: null,
  },
  parser_id: 'general',
  embedding_model: '',
  document_count: 1,
  chunk_count: 3,
  created_at: '2026-11-11T00:00:00Z',
  updated_at: '2026-11-11T00:00:00Z',
}
const document = {
  id: 'doc-1', kb_id: 'kb-1', name: '制度.txt', mime: 'text/plain', size: 10, sha256: 'x', status: 'parsing',
  error_code: null, progress: 0.5, error_message: null, chunk_count: 0, token_count: 0, created_at: '', updated_at: '',
}

afterEach(() => vi.unstubAllGlobals())

describe('知识库接口', () => {
  it('列表、详情与创建校验返回结构', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([base]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(base), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(base), { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    expect(await listKnowledgeBases()).toEqual([base])
    expect(await getKnowledgeBase('kb-1')).toEqual(base)
    const input = { name: '制度库', description: '', visibility: 'workspace' as const, parser_settings: base.parser_settings, parser_id: 'general', embedding_model: '', language: '' }
    expect(await createKnowledgeBase(input)).toEqual(base)
    expect(JSON.parse(fetcher.mock.calls[2]?.[1]?.body as string)).toEqual(input)
  })

  it('上传使用 FormData 且不手写 Content-Type', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(document), { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    const file = new File(['x'], '制度.txt', { type: 'text/plain' })
    expect((await uploadKnowledgeDocument('kb-1', file)).id).toBe('doc-1')
    const init = fetcher.mock.calls[0]?.[1] as RequestInit
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('file')).toBe(file)
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined()
  })

  it('重新解析可以携带解析覆盖', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(document), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    const settings = { ...base.parser_settings, chunk_token_size: 128 }
    await parseKnowledgeDocument('doc-1', settings)
    expect(JSON.parse(fetcher.mock.calls[0]?.[1]?.body as string)).toEqual({ parser_settings: settings })
  })

  it('分块、检索测试与批量重解析使用新端点', async () => {
    const page = { total: 30, offset: 0, limit: 20, chunks: [{ content: 'chunk 0', available: true }] }
    const retrieval = { chunks: [{ content: '命中', document_id: 'doc-1', document_name: '制度.txt', similarity: 0.9, positions: [1] }] }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(page), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(retrieval), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([document]), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    expect((await listDocumentChunks('doc-1')).total).toBe(30)
    const result = await retrievalTest('kb-1', { query: '年假', top_k: 5, threshold: 0.2 })
    expect(result.chunks[0].similarity).toBe(0.9)
    expect((await reparseAllDocuments('kb-1'))[0]?.id).toBe('doc-1')
    expect(String(fetcher.mock.calls[0]?.[0])).toContain('/knowledge-documents/doc-1/chunks?offset=0&limit=20')
    expect(String(fetcher.mock.calls[1]?.[0])).toContain('/knowledge-bases/kb-1/retrieval-test')
    expect(String(fetcher.mock.calls[2]?.[0])).toContain('/knowledge-bases/kb-1/reparse-documents')
  })

  it('原文件 URL 携带当前空间参数', () => {
    setSecurityContext('csrf', 'w1')
    expect(documentFileUrl('doc-1')).toContain('/knowledge-documents/doc-1/file?workspace_id=w1')
  })

  it('非法响应不会被当作成功', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([{ ...base, visibility: 'unknown' }]), { status: 200 })))
    await expect(listKnowledgeBases()).rejects.toThrow('格式不正确')
  })
})
