import { ApiError, requestJson } from './client'
import { parseSnapshot, type Snapshot } from './chat'

export interface Schedule {
  id: string
  conversation_id: string
  conversation_title: string
  name: string
  prompt: string
  cron_expression: string
  timezone: string
  enabled: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_run_id: string | null
  last_status: string | null
  last_error_code: string | null
  created_at: string
  updated_at: string
}

export interface ScheduleInput {
  name: string
  prompt: string
  conversation_id: string
  cron_expression: string
  timezone: string
  enabled: boolean
}

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function nullableString(value: unknown): value is string | null { return value === null || typeof value === 'string' }
function invalid(): never { throw new ApiError('定时任务接口返回格式不正确', 502) }

function parseSchedule(value: unknown): Schedule {
  if (!record(value) || typeof value.id !== 'string' || typeof value.conversation_id !== 'string' || typeof value.conversation_title !== 'string' || typeof value.name !== 'string' || typeof value.prompt !== 'string' || typeof value.cron_expression !== 'string' || typeof value.timezone !== 'string' || typeof value.enabled !== 'boolean' || !nullableString(value.next_run_at) || !nullableString(value.last_run_at) || !nullableString(value.last_run_id) || !nullableString(value.last_status) || !nullableString(value.last_error_code) || typeof value.created_at !== 'string' || typeof value.updated_at !== 'string') return invalid()
  return value as unknown as Schedule
}

export async function listSchedules(offset = 0): Promise<Schedule[]> {
  const body = await requestJson('/api/schedules?offset=' + offset + '&limit=50')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseSchedule)
}

export async function createSchedule(input: ScheduleInput): Promise<Schedule> {
  return parseSchedule(await requestJson('/api/schedules', { method: 'POST', body: JSON.stringify(input) }))
}

export async function updateSchedule(id: string, input: ScheduleInput): Promise<Schedule> {
  return parseSchedule(await requestJson('/api/schedules/' + encodeURIComponent(id), { method: 'POST', body: JSON.stringify(input) }))
}

export async function deleteSchedule(id: string): Promise<void> {
  await requestJson('/api/schedules/' + encodeURIComponent(id) + '/delete', { method: 'POST' })
}

export async function runScheduleNow(id: string): Promise<Snapshot> {
  return parseSnapshot(await requestJson('/api/schedules/' + encodeURIComponent(id) + '/run', { method: 'POST' }))
}
