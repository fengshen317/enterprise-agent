import { ApiError, requestJson } from './client'

export type ToolRisk = 'read' | 'write' | 'delete' | 'external'
export type ToolCallStatus = 'awaiting_approval' | 'running' | 'completed' | 'failed' | 'cancelled' | 'denied' | 'expired'

export interface Tool {
  key: string
  version: number
  display_name: string
  description: string
  risk: ToolRisk
  approval: 'never' | 'always'
  timeout_seconds: number
  quota_per_run: number
  input_schema: Record<string, unknown>
  enabled: boolean
  used_by_agents: number
}

export interface ToolCall {
  id: string
  run_id: string
  tool_name: string
  tool_version?: number | null
  risk_level?: string | null
  approval_required?: boolean
  arguments_digest?: string | null
  days: number | null
  status: ToolCallStatus
  result: string | null
  error_code: string | null
  created_at: string
  finished_at: string | null
}

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('服务返回的数据格式不正确', 502) }
export function parseTool(value: unknown): Tool {
  if (!record(value) || typeof value.key !== 'string' || typeof value.version !== 'number'
    || typeof value.display_name !== 'string' || typeof value.description !== 'string'
    || !['read', 'write', 'delete', 'external'].includes(String(value.risk))
    || (value.approval !== 'never' && value.approval !== 'always')
    || typeof value.timeout_seconds !== 'number' || typeof value.quota_per_run !== 'number'
    || !record(value.input_schema) || typeof value.enabled !== 'boolean' || typeof value.used_by_agents !== 'number') return invalid()
  return value as unknown as Tool
}
export async function listTools(): Promise<Tool[]> {
  const body = await requestJson('/api/tools')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseTool)
}
export async function setToolEnabled(key: string, enabled: boolean): Promise<Tool> {
  return parseTool(await requestJson('/api/admin/tools/' + encodeURIComponent(key), { method: 'POST', body: JSON.stringify({ enabled }) }))
}
export async function getToolCalls(runId: string): Promise<ToolCall[]> {
  const body = await requestJson('/api/runs/' + encodeURIComponent(runId) + '/tool-calls')
  if (!Array.isArray(body) || !body.every((item: unknown) => {
    if (!record(item)) return false
    return typeof item.id === 'string' && item.run_id === runId && typeof item.tool_name === 'string'
      && (item.tool_version === null || typeof item.tool_version === 'number')
      && (item.risk_level === null || typeof item.risk_level === 'string')
      && typeof item.approval_required === 'boolean'
      && (item.arguments_digest === null || typeof item.arguments_digest === 'string')
      && (item.days === null || typeof item.days === 'number')
      && ['awaiting_approval', 'running', 'completed', 'failed', 'cancelled', 'denied', 'expired'].includes(String(item.status))
      && (item.result === null || typeof item.result === 'string')
      && (item.error_code === null || typeof item.error_code === 'string')
      && typeof item.created_at === 'string' && (item.finished_at === null || typeof item.finished_at === 'string')
  })) throw new ApiError('工具记录格式不正确，请重新加载', 502)
  return body as ToolCall[]
}
