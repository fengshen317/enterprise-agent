import { ApiError, requestJson } from './client'
import { parseSnapshot, type Snapshot } from './chat'

export const scheduleCalendars = {
  all_days: '不筛选日期',
  cn_workday: '中国法定工作日',
  cn_a_share: '沪深 A 股交易日',
} as const
export type ScheduleCalendar = keyof typeof scheduleCalendars

export interface Schedule {
  id: string
  conversation_id: string
  conversation_title: string
  name: string
  prompt: string
  cron_expression: string
  timezone: string
  calendar_mode: ScheduleCalendar
  calendar_waiting_year: number | null
  revision: number
  enabled: boolean
  push_to_wechat: boolean
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
  calendar_mode?: ScheduleCalendar
  revision?: number
  enabled: boolean
  push_to_wechat: boolean
}

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function nullableString(value: unknown): value is string | null { return value === null || typeof value === 'string' }
function invalid(): never { throw new ApiError('定时任务接口返回格式不正确', 502) }

function parseSchedule(value: unknown): Schedule {
  if (!record(value) || typeof value.id !== 'string' || typeof value.conversation_id !== 'string' || typeof value.conversation_title !== 'string' || typeof value.name !== 'string' || typeof value.prompt !== 'string' || typeof value.cron_expression !== 'string' || typeof value.timezone !== 'string' || typeof value.enabled !== 'boolean' || typeof value.push_to_wechat !== 'boolean' || !nullableString(value.next_run_at) || !nullableString(value.last_run_at) || !nullableString(value.last_run_id) || !nullableString(value.last_status) || !nullableString(value.last_error_code) || typeof value.created_at !== 'string' || typeof value.updated_at !== 'string') return invalid()
  if (typeof value.calendar_mode !== 'string' || !Object.hasOwn(scheduleCalendars, value.calendar_mode)
    || (value.calendar_waiting_year !== null && !Number.isInteger(value.calendar_waiting_year))
    || typeof value.revision !== 'number' || !Number.isInteger(value.revision) || value.revision < 1) return invalid()
  return value as unknown as Schedule
}

export async function listSchedules(offset = 0, limit = 50): Promise<Schedule[]> {
  const body = await requestJson('/api/schedules?offset=' + offset + '&limit=' + limit)
  if (!Array.isArray(body)) return invalid()
  return body.map(parseSchedule)
}

export async function updateScheduleEnabled(id: string, enabled: boolean): Promise<Schedule> {
  return parseSchedule(await requestJson('/api/schedules/' + encodeURIComponent(id) + '/enabled', {
    method: 'POST', body: JSON.stringify({ enabled }),
  }))
}

export async function getSchedule(id: string): Promise<Schedule> {
  return parseSchedule(await requestJson('/api/schedules/' + encodeURIComponent(id)))
}

export async function createSchedule(input: ScheduleInput): Promise<Schedule> {
  return parseSchedule(await requestJson('/api/schedules', { method: 'POST', body: JSON.stringify(input) }))
}

export async function updateSchedule(id: string, input: ScheduleInput): Promise<Schedule> {
  return parseSchedule(await requestJson('/api/schedules/' + encodeURIComponent(id), { method: 'POST', body: JSON.stringify(input) }))
}

export async function updateScheduleWeChatPush(id: string, enabled: boolean): Promise<Schedule> {
  return parseSchedule(await requestJson('/api/schedules/' + encodeURIComponent(id) + '/wechat-push', {
    method: 'POST',
    body: JSON.stringify({ push_to_wechat: enabled }),
  }))
}

export async function deleteSchedule(id: string): Promise<void> {
  await requestJson('/api/schedules/' + encodeURIComponent(id) + '/delete', { method: 'POST' })
}

export async function runScheduleNow(id: string): Promise<Snapshot> {
  return parseSnapshot(await requestJson('/api/schedules/' + encodeURIComponent(id) + '/run', { method: 'POST' }))
}
