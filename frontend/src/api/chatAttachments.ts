import { API_BASE_URL, ApiError, getWorkspaceId } from './client'

export interface ChatAttachment {
  id: string
  filename: string
  media_type: string
  size_bytes: number
  text_available: boolean
  text_truncated: boolean
}

export const attachmentAccept = '.png,.jpg,.jpeg,.webp,.pdf,.docx,.xlsx,.txt,.md,.csv,.json'
export const maxAttachmentCount = 4
export const maxAttachmentBytes = 8 * 1024 ** 2
export const maxTotalAttachmentBytes = 16 * 1024 ** 2
export const attachmentOnlyQuestion = '请帮我查看这些附件。'

export function isAttachmentImage(type: string): boolean {
  return ['image/png', 'image/jpeg', 'image/webp'].includes(type)
}

export function parseChatAttachment(value: unknown): ChatAttachment {
  if (typeof value !== 'object' || value === null) throw new ApiError('附件信息格式不正确', 502)
  const item = value as Record<string, unknown>
  if (typeof item.id !== 'string' || typeof item.filename !== 'string' || typeof item.media_type !== 'string'
    || typeof item.size_bytes !== 'number' || !Number.isInteger(item.size_bytes) || item.size_bytes < 1
    || typeof item.text_available !== 'boolean' || typeof item.text_truncated !== 'boolean') {
    throw new ApiError('附件信息格式不正确', 502)
  }
  return item as unknown as ChatAttachment
}

export function chatAttachmentUrl(conversationId: string, id: string, download = false): string {
  const query = new URLSearchParams({ workspace_id: getWorkspaceId() })
  if (download) query.set('download', 'true')
  return `${API_BASE_URL}/api/conversations/${encodeURIComponent(conversationId)}/attachments/${encodeURIComponent(id)}/file?${query}`
}

export function attachmentSizeLabel(bytes: number): string {
  const kib = 1024
  if (bytes < kib) return `${bytes} B`
  if (bytes < kib ** 2) return `${(bytes / kib).toFixed(1)} KB`
  return `${(bytes / kib ** 2).toFixed(1)} MB`
}
