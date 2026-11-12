import { API_BASE_URL, ApiError, getWorkspaceId, requestForm, requestJson } from './client'

export type KnowledgeVisibility = 'workspace' | 'private'
export type PdfParseMethod = 'deepdoc' | 'mineru'
export interface ParserSettings {
  chunk_token_size: number
  delimiters: string[]
  overlapped_percent: number
  use_parent_child: boolean
  parent_child_delimiter: string
  pdf_parse_method: PdfParseMethod | null
  auto_keywords_top_n: number | null
  auto_questions_top_n: number | null
}
export interface KnowledgeBase {
  id: string
  name: string
  description: string
  visibility: KnowledgeVisibility
  status: string
  error_code: string | null
  parser_settings: ParserSettings
  parser_id: string
  embedding_model: string
  document_count: number
  chunk_count: number
  created_at: string
  updated_at: string
}
export interface KnowledgeBaseInput {
  name: string
  description: string
  visibility: KnowledgeVisibility
  parser_settings: ParserSettings
  parser_id: string
  embedding_model: string
  language: string
}
export interface KnowledgeDocument {
  id: string
  kb_id: string
  name: string
  mime: string
  size: number
  sha256: string
  status: string
  error_code: string | null
  progress: number
  error_message: string | null
  chunk_count: number
  token_count: number
  created_at: string
  updated_at: string
}
export interface KnowledgeChunkItem {
  content: string
  available: boolean
}
export interface KnowledgeChunkPage {
  total: number
  offset: number
  limit: number
  chunks: KnowledgeChunkItem[]
}
export interface RetrievalTestInput {
  query: string
  top_k: number
  threshold: number
}
export interface RetrievalTestChunk {
  content: string
  document_id: string
  document_name: string
  similarity: number | null
  positions: unknown[]
}
export interface RetrievalTestResult {
  chunks: RetrievalTestChunk[]
}

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('知识库接口返回格式不正确', 502) }
function nullableString(value: unknown): value is string | null { return value === null || typeof value === 'string' }
function nullableNumber(value: unknown): value is number | null { return value === null || typeof value === 'number' }
function nullableMethod(value: unknown): value is PdfParseMethod | null { return value === null || value === 'deepdoc' || value === 'mineru' }

function parseParser(value: unknown): ParserSettings {
  if (!record(value) || typeof value.chunk_token_size !== 'number' || !Array.isArray(value.delimiters) || !value.delimiters.every(item => typeof item === 'string') || typeof value.overlapped_percent !== 'number' || typeof value.use_parent_child !== 'boolean' || typeof value.parent_child_delimiter !== 'string' || !nullableMethod(value.pdf_parse_method) || !nullableNumber(value.auto_keywords_top_n) || !nullableNumber(value.auto_questions_top_n)) return invalid()
  return value as unknown as ParserSettings
}

function parseBase(value: unknown): KnowledgeBase {
  if (!record(value) || typeof value.id !== 'string' || typeof value.name !== 'string' || typeof value.description !== 'string' || !['workspace', 'private'].includes(String(value.visibility)) || typeof value.status !== 'string' || !nullableString(value.error_code) || typeof value.parser_id !== 'string' || typeof value.embedding_model !== 'string' || typeof value.document_count !== 'number' || typeof value.chunk_count !== 'number' || typeof value.created_at !== 'string' || typeof value.updated_at !== 'string') return invalid()
  return { ...(value as unknown as KnowledgeBase), parser_settings: parseParser(value.parser_settings) }
}

function parseDocument(value: unknown): KnowledgeDocument {
  if (!record(value) || typeof value.id !== 'string' || typeof value.kb_id !== 'string' || typeof value.name !== 'string' || typeof value.mime !== 'string' || typeof value.size !== 'number' || typeof value.sha256 !== 'string' || typeof value.status !== 'string' || !nullableString(value.error_code) || typeof value.progress !== 'number' || !nullableString(value.error_message) || typeof value.chunk_count !== 'number' || typeof value.token_count !== 'number' || typeof value.created_at !== 'string' || typeof value.updated_at !== 'string') return invalid()
  return value as unknown as KnowledgeDocument
}

function parseChunkPage(value: unknown): KnowledgeChunkPage {
  if (!record(value) || typeof value.total !== 'number' || typeof value.offset !== 'number' || typeof value.limit !== 'number' || !Array.isArray(value.chunks) || !value.chunks.every(item => record(item) && typeof item.content === 'string' && typeof item.available === 'boolean')) return invalid()
  return value as unknown as KnowledgeChunkPage
}

function parseRetrieval(value: unknown): RetrievalTestResult {
  if (!record(value) || !Array.isArray(value.chunks) || !value.chunks.every(item => record(item) && typeof item.content === 'string' && typeof item.document_id === 'string' && typeof item.document_name === 'string' && nullableNumber(item.similarity) && Array.isArray(item.positions))) return invalid()
  return value as unknown as RetrievalTestResult
}

export async function listKnowledgeBases(offset = 0): Promise<KnowledgeBase[]> {
  const body = await requestJson('/api/knowledge-bases?offset=' + offset + '&limit=50')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseBase)
}

export async function getKnowledgeBase(id: string): Promise<KnowledgeBase> {
  return parseBase(await requestJson('/api/knowledge-bases/' + encodeURIComponent(id)))
}

export async function createKnowledgeBase(input: KnowledgeBaseInput): Promise<KnowledgeBase> {
  return parseBase(await requestJson('/api/knowledge-bases', { method: 'POST', body: JSON.stringify(input) }))
}

export async function updateKnowledgeBase(id: string, input: KnowledgeBaseInput): Promise<KnowledgeBase> {
  return parseBase(await requestJson('/api/knowledge-bases/' + encodeURIComponent(id), { method: 'POST', body: JSON.stringify(input) }))
}

export async function deleteKnowledgeBase(id: string): Promise<void> {
  await requestJson('/api/knowledge-bases/' + encodeURIComponent(id) + '/delete', { method: 'POST' })
}

export async function listKnowledgeDocuments(kbId: string): Promise<KnowledgeDocument[]> {
  const body = await requestJson('/api/knowledge-bases/' + encodeURIComponent(kbId) + '/documents')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseDocument)
}

export async function uploadKnowledgeDocument(kbId: string, file: File): Promise<KnowledgeDocument> {
  const form = new FormData()
  form.append('file', file)
  return parseDocument(await requestForm('/api/knowledge-bases/' + encodeURIComponent(kbId) + '/documents', form))
}

export async function parseKnowledgeDocument(id: string, parserSettings?: ParserSettings): Promise<KnowledgeDocument> {
  const body = parserSettings ? { parser_settings: parserSettings } : {}
  return parseDocument(await requestJson('/api/knowledge-documents/' + encodeURIComponent(id) + '/parse', { method: 'POST', body: JSON.stringify(body) }))
}

export async function deleteKnowledgeDocument(id: string): Promise<void> {
  await requestJson('/api/knowledge-documents/' + encodeURIComponent(id) + '/delete', { method: 'POST' })
}

export function documentFileUrl(id: string): string {
  return API_BASE_URL + '/api/knowledge-documents/' + encodeURIComponent(id) + '/file?workspace_id=' + encodeURIComponent(getWorkspaceId())
}

export async function listDocumentChunks(id: string, offset = 0, limit = 20): Promise<KnowledgeChunkPage> {
  const body = await requestJson('/api/knowledge-documents/' + encodeURIComponent(id) + '/chunks?offset=' + offset + '&limit=' + limit)
  return parseChunkPage(body)
}

export async function retrievalTest(kbId: string, input: RetrievalTestInput): Promise<RetrievalTestResult> {
  return parseRetrieval(await requestJson('/api/knowledge-bases/' + encodeURIComponent(kbId) + '/retrieval-test', { method: 'POST', body: JSON.stringify(input) }, 20000))
}

export async function reparseAllDocuments(kbId: string): Promise<KnowledgeDocument[]> {
  const body = await requestJson('/api/knowledge-bases/' + encodeURIComponent(kbId) + '/reparse-documents', { method: 'POST', body: JSON.stringify({}) }, 30000)
  if (!Array.isArray(body)) return invalid()
  return body.map(parseDocument)
}
