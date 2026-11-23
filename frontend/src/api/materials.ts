import { API_BASE_URL, ApiError, getWorkspaceId, requestForm, requestJson } from './client'
import { parseChatAttachment, type ChatAttachment } from './chatAttachments'

export interface Material extends ChatAttachment {
  created_at: string
  source_conversation_id: string | null
  excerpt: string
}

function parseMaterial(value: unknown): Material {
  const metadata = parseChatAttachment(value)
  const item = value as Record<string, unknown>
  if (typeof item.created_at !== 'string' || typeof item.excerpt !== 'string'
    || (item.source_conversation_id !== null && typeof item.source_conversation_id !== 'string')) {
    throw new ApiError('材料信息格式不正确', 502)
  }
  return { ...metadata, created_at: item.created_at, source_conversation_id: item.source_conversation_id, excerpt: item.excerpt }
}

export async function listMaterials(query: string, offset: number, limit: number): Promise<{ items: Material[]; has_more: boolean }> {
  const params = new URLSearchParams({ q: query, offset: String(offset), limit: String(limit) })
  const body = await requestJson('/api/materials?' + params)
  if (typeof body !== 'object' || body === null || !('items' in body) || !Array.isArray(body.items)
    || !('has_more' in body) || typeof body.has_more !== 'boolean') throw new ApiError('材料列表格式不正确', 502)
  return { items: body.items.map(parseMaterial), has_more: body.has_more }
}

export async function getMaterial(id: string): Promise<Material> {
  return parseMaterial(await requestJson('/api/materials/' + encodeURIComponent(id)))
}

export async function uploadMaterials(files: File[]): Promise<Material[]> {
  const form = new FormData()
  for (const file of files) form.append('files', file, file.name)
  const body = await requestForm('/api/materials', form)
  if (!Array.isArray(body)) throw new ApiError('上传结果格式不正确', 502)
  return body.map(parseMaterial)
}

export function materialFileUrl(id: string, download = false): string {
  const query = new URLSearchParams({ workspace_id: getWorkspaceId() })
  if (download) query.set('download', 'true')
  return `${API_BASE_URL}/api/materials/${encodeURIComponent(id)}/file?${query}`
}
