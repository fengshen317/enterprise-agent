import { API_BASE_URL, ApiError, getWorkspaceId, notifyUnauthorized, requestJson } from './client'

export type TaskStatus = 'pending' | 'planning' | 'awaiting_confirmation' | 'running' | 'retry_wait' | 'paused' | 'completed' | 'failed' | 'cancelled'
export type StepStatus = 'pending' | 'running' | 'retry_wait' | 'paused' | 'succeeded' | 'failed' | 'cancelled' | 'skipped'
export type StepKind = 'analysis' | 'tool' | 'synthesis'
export type TaskControl = 'pause' | 'resume' | 'cancel' | 'retry'
export interface PlanStep { key: string; title: string; kind: StepKind; depends_on: string[]; instruction: string; tool_name: string | null; arguments: Record<string, unknown> }
export interface TaskStep { key: string; title: string; kind: StepKind; depends_on: string[]; status: StepStatus; attempt_count: number; result: string | null; error_code: string | null; next_retry_at: string | null }
export interface TaskAction { id: string; step_key: string | null; kind: string; status: string; attempt_count: number; max_attempts: number; error_code: string | null; next_retry_at: string | null }
export interface TaskSnapshot {
  task: { id: string; goal: string; workspace_id: string; owner_user_id: string; created_at: string }
  run: { id: string; status: TaskStatus; error_code: string | null; result: string | null; deadline_at: string | null; next_retry_at: string | null; fencing_token: number; lease_owner: string | null; version: number }
  plan: { revision: number; digest: string; steps: PlanStep[] } | null
  steps: TaskStep[]
  actions: TaskAction[]
  last_event_sequence: number
}
const TASK_STATUSES: TaskStatus[] = ['pending', 'planning', 'awaiting_confirmation', 'running', 'retry_wait', 'paused', 'completed', 'failed', 'cancelled']
const STEP_STATUSES: StepStatus[] = ['pending', 'running', 'retry_wait', 'paused', 'succeeded', 'failed', 'cancelled', 'skipped']
function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function nullableString(value: unknown): boolean { return value === null || typeof value === 'string' }
function invalid(): never { throw new ApiError('任务响应格式不正确，请重新读取', 502) }
function strings(value: unknown): value is string[] { return Array.isArray(value) && value.every(item => typeof item === 'string') }
function kind(value: unknown): value is StepKind { return ['analysis', 'tool', 'synthesis'].includes(String(value)) }
export function parseTask(value: unknown): TaskSnapshot {
  if (!record(value) || !record(value.task) || !record(value.run) || !Array.isArray(value.steps)) return invalid()
  const task = value.task; const run = value.run
  if (!['id', 'goal', 'workspace_id', 'owner_user_id', 'created_at'].every(key => typeof task[key] === 'string')) return invalid()
  if (typeof run.id !== 'string' || !TASK_STATUSES.includes(run.status as TaskStatus) || !['error_code', 'result', 'deadline_at', 'next_retry_at', 'lease_owner'].every(key => nullableString(run[key])) || !['version', 'fencing_token'].every(key => typeof run[key] === 'number')) return invalid()
  if (value.plan !== null) {
    const plan = value.plan
    if (!record(plan) || typeof plan.revision !== 'number' || typeof plan.digest !== 'string' || !Array.isArray(plan.steps)) return invalid()
    for (const step of plan.steps) {
      if (!record(step) || !['key', 'title', 'instruction'].every(key => typeof step[key] === 'string') || !kind(step.kind) || !strings(step.depends_on) || !nullableString(step.tool_name) || !record(step.arguments)) return invalid()
    }
  }
  for (const step of value.steps) {
    if (!record(step) || typeof step.key !== 'string' || typeof step.title !== 'string' || !kind(step.kind) || !strings(step.depends_on) || !STEP_STATUSES.includes(step.status as StepStatus) || typeof step.attempt_count !== 'number' || !['result', 'error_code', 'next_retry_at'].every(key => nullableString(step[key]))) return invalid()
  }
  if (!Array.isArray(value.actions)) return invalid()
  for (const action of value.actions) {
    if (!record(action) || typeof action.id !== 'string' || !nullableString(action.step_key) || !['plan', 'model', 'tool'].includes(String(action.kind)) || !['pending', 'running', 'retry_wait', 'succeeded', 'failed', 'interrupted', 'cancelled', 'paused'].includes(String(action.status)) || !Number.isInteger(action.attempt_count) || Number(action.attempt_count) < 0 || !Number.isInteger(action.max_attempts) || Number(action.max_attempts) < 1 || !nullableString(action.error_code) || !nullableString(action.next_retry_at)) return invalid()
  }
  if (typeof value.last_event_sequence !== 'number') return invalid()
  return value as unknown as TaskSnapshot
}
export function isTerminal(status: TaskStatus): boolean { return ['completed', 'failed', 'cancelled'].includes(status) }
export async function listTasks(offset = 0): Promise<TaskSnapshot[]> {
  const value = await requestJson('/api/tasks?offset=' + offset + '&limit=20')
  if (!Array.isArray(value)) return invalid()
  return value.map(parseTask)
}
export async function createTask(goal: string, idempotencyKey: string, sourceConversationId?: string): Promise<TaskSnapshot> {
  return parseTask(await requestJson('/api/tasks', { method: 'POST', body: JSON.stringify({ goal, idempotency_key: idempotencyKey, ...(sourceConversationId ? { source_conversation_id: sourceConversationId } : {}) }) }))
}
export async function getTask(id: string): Promise<TaskSnapshot> { return parseTask(await requestJson('/api/tasks/' + encodeURIComponent(id))) }
export async function confirmTask(id: string, revision: number, digest: string, runId: string): Promise<TaskSnapshot> {
  return parseTask(await requestJson('/api/tasks/' + encodeURIComponent(id) + '/confirm', { method: 'POST', body: JSON.stringify({ revision, digest, run_id: runId }) }))
}
export async function controlTask(id: string, action: TaskControl, runId: string, version: number): Promise<TaskSnapshot> {
  return parseTask(await requestJson('/api/tasks/' + encodeURIComponent(id) + '/' + action, { method: 'POST', body: JSON.stringify({ run_id: runId, version }) }))
}

export async function updateTaskPlan(id: string, definition: unknown, runId: string, revision: number): Promise<TaskSnapshot> {
  if (!record(definition) || !Array.isArray(definition.steps)) throw new ApiError('计划需要包含 steps 数组', 422)
  // 版本和执行标识来自当前可信页面快照，不能被编辑器内容覆盖。
  const body = { steps: definition.steps, run_id: runId, revision }
  return parseTask(await requestJson('/api/tasks/' + encodeURIComponent(id) + '/plan', { method: 'POST', body: JSON.stringify(body) }))
}
export function watchTask(id: string, afterSequence: number, receive: (value: TaskSnapshot) => void, connection: (message: string) => void): () => void {
  const source = new EventSource(API_BASE_URL + '/api/tasks/' + encodeURIComponent(id) + '/events?workspace_id=' + encodeURIComponent(getWorkspaceId()) + '&after_sequence=' + afterSequence, { withCredentials: true })
  let disposed = false
  source.addEventListener('snapshot', (event: MessageEvent<string>) => {
    if (disposed) return
    try {
      const value = parseTask(JSON.parse(event.data) as unknown)
      if (value.task.id !== id) throw new Error('任务不匹配')
      connection('')
      receive(value)
      if (isTerminal(value.run.status)) source.close()
    } catch { source.close(); connection('进度数据异常，请重新读取') }
  })
  source.addEventListener('access_revoked', () => { if (!disposed) { source.close(); notifyUnauthorized() } })
  source.onerror = () => { if (!disposed) connection('连接中断，正在重新读取进度') }
  return () => { disposed = true; source.close() }
}
