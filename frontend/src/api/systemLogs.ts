import { ApiError, getJson } from './client'

export const logComponents = ['http', 'run', 'tool', 'model', 'connector', 'schedule', 'wechat'] as const
export const logOutcomes = ['success', 'warning', 'error'] as const
export const logLevels = ['info', 'warning', 'error'] as const
export const logDetailKeys = [
  'phase', 'upstream_status', 'upstream_code', 'rule', 'route', 'method',
  'status_code', 'attempt', 'max_attempts', 'channel', 'run_status',
] as const

export type LogComponent = typeof logComponents[number]
export type LogOutcome = typeof logOutcomes[number]
export type LogLevel = typeof logLevels[number]
export type LogDays = 1 | 7 | 30
export type LogDetailKey = typeof logDetailKeys[number]
export type LogDetails = Partial<Record<LogDetailKey, string | number | null>>

export interface SystemLogEvent {
  id: string
  occurred_at: string
  level: LogLevel
  component: LogComponent
  event: string
  outcome: LogOutcome
  error_code: string | null
  duration_ms: number | null
  request_id: string | null
  run_id: string | null
  tool_call_id: string | null
  connector_key: string | null
  tool_name: string | null
  task_execution_id: string | null
  details?: LogDetails | null
}

export interface SystemLogFilters {
  days: LogDays
  q?: string
  component?: LogComponent
  outcome?: LogOutcome
  level?: LogLevel
  cursor?: string
  limit: number
}

export interface SystemLogPage {
  items: SystemLogEvent[]
  total: number
  counts: Record<LogOutcome | 'all', number>
  next_cursor: string | null
}

export interface SystemLogDetail {
  event: SystemLogEvent
  related: SystemLogEvent[]
  related_truncated: boolean
}

const optionalIdentifiers = [
  'error_code', 'request_id', 'run_id', 'tool_call_id', 'connector_key', 'tool_name', 'task_execution_id',
] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function invalid(): never {
  throw new ApiError('系统日志接口返回格式不正确', 502)
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function parseEvent(value: unknown): SystemLogEvent {
  if (!isRecord(value)
    || typeof value.id !== 'string' || typeof value.event !== 'string'
    || typeof value.occurred_at !== 'string' || !Number.isFinite(Date.parse(value.occurred_at))
    || !logComponents.some(component => component === value.component)
    || !logLevels.some(level => level === value.level) || !logOutcomes.some(outcome => outcome === value.outcome)
    || (value.duration_ms != null && !isCount(value.duration_ms))
    || !optionalIdentifiers.every(key => value[key] == null || typeof value[key] === 'string')
    || (value.details != null && !isRecord(value.details))) return invalid()
  const fields = Object.fromEntries(optionalIdentifiers.map(key => [key, value[key] ?? null]))
  const details: LogDetails = {}
  if (isRecord(value.details)) {
    for (const key of logDetailKeys) {
      const detail = value.details[key]
      if (typeof detail === 'string' || typeof detail === 'number' || detail === null) details[key] = detail
    }
  }
  return {
    id: value.id, occurred_at: value.occurred_at, event: value.event,
    level: value.level, component: value.component, outcome: value.outcome,
    duration_ms: value.duration_ms ?? null, ...fields, details,
  } as SystemLogEvent
}

export async function listSystemLogs(filters: SystemLogFilters): Promise<SystemLogPage> {
  const { days, limit, component, outcome, level, q, cursor } = filters
  const query = new URLSearchParams({ days: String(days), limit: String(limit) })
  for (const [key, value] of Object.entries({ component, outcome, level, q, cursor })) {
    if (value) query.set(key, value)
  }
  const body = await getJson('/api/admin/system-logs?' + query)
  if (!isRecord(body) || !Array.isArray(body.items) || !isCount(body.total)
    || (body.next_cursor !== null && typeof body.next_cursor !== 'string') || !isRecord(body.counts)) return invalid()
  const { all, success, warning, error } = body.counts
  if (!isCount(all) || !isCount(success) || !isCount(warning) || !isCount(error)) return invalid()
  return { items: body.items.map(parseEvent), total: body.total, next_cursor: body.next_cursor, counts: { all, success, warning, error } }
}

export async function getSystemLog(id: string): Promise<SystemLogDetail> {
  const body = await getJson('/api/admin/system-logs/' + encodeURIComponent(id))
  if (!isRecord(body) || !Array.isArray(body.related) || typeof body.related_truncated !== 'boolean') return invalid()
  return { event: parseEvent(body.event), related: body.related.map(parseEvent), related_truncated: body.related_truncated }
}
