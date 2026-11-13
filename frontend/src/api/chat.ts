import { API_BASE_URL, ApiError, requestJson, getWorkspaceId, notifyUnauthorized } from './client'
import { parseApproval, type Approval } from './approvals'

export interface Conversation { id: string; title: string; status: string; agent_profile_id?: string | null; updated_at: string }
export interface Message { id: string; conversation_id: string; run_id: string | null; sequence: number; role: 'user' | 'assistant'; content: string; status: string }
export type RunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
export interface Run { id: string; conversation_id: string; status: RunStatus; model: string; error_code: string | null; error_message: string | null }
export interface Snapshot { run: Run; message: Message; approval?: Approval | null }
export interface MessagePage { items: Message[]; has_more: boolean; active_run_id: string | null }
const RUN_STATUSES = ['pending', 'running', 'completed', 'failed', 'cancelled']

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('服务返回的数据格式不正确', 502) }
function parseConversation(value: unknown): Conversation {
  if (!record(value) || typeof value.id !== 'string' || typeof value.title !== 'string' || typeof value.status !== 'string' || typeof value.updated_at !== 'string') return invalid()
  const agent = value.agent_profile_id
  if (!(agent === undefined || agent === null || typeof agent === 'string')) return invalid()
  return { ...value as unknown as Conversation, agent_profile_id: agent ?? null }
}
function parseMessage(value: unknown): Message {
  if (!record(value) || typeof value.id !== 'string' || typeof value.conversation_id !== 'string' || !(value.run_id === null || typeof value.run_id === 'string') || typeof value.sequence !== 'number' || !['user', 'assistant'].includes(String(value.role)) || typeof value.content !== 'string' || !['streaming', 'completed', 'interrupted'].includes(String(value.status))) return invalid()
  return value as unknown as Message
}
export function parseSnapshot(value: unknown): Snapshot {
  if (!record(value) || !record(value.run)) return invalid()
  const run = value.run
  if (typeof run.id !== 'string' || typeof run.conversation_id !== 'string' || !RUN_STATUSES.includes(String(run.status)) || typeof run.model !== 'string' || !(run.error_code === null || typeof run.error_code === 'string') || !(run.error_message === null || typeof run.error_message === 'string')) return invalid()
  const message = parseMessage(value.message)
  if (message.run_id !== run.id || message.conversation_id !== run.conversation_id) return invalid()
  const approval = value.approval === undefined || value.approval === null ? null : parseApproval(value.approval)
  return approval ? { run: run as unknown as Run, message, approval } : { run: run as unknown as Run, message }
}
export function isActive(run: Run): boolean { return run.status === 'pending' || run.status === 'running' }
export async function listConversations(offset = 0): Promise<Conversation[]> {
  const body = await requestJson('/api/conversations?offset=' + offset + '&limit=50')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseConversation)
}
export async function createConversation(title = '新对话', agentProfileId: string | null = null): Promise<Conversation> {
  return parseConversation(await requestJson('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ title, agent_profile_id: agentProfileId }),
  }))
}
export async function bindConversationAgent(id: string, agentProfileId: string | null): Promise<Conversation> {
  return parseConversation(await requestJson('/api/conversations/' + encodeURIComponent(id) + '/agent', {
    method: 'POST',
    body: JSON.stringify({ agent_profile_id: agentProfileId }),
  }))
}
export async function getMessages(id: string, afterSequence = 0): Promise<MessagePage> {
  const body = await requestJson('/api/conversations/' + encodeURIComponent(id) + '/messages?after_sequence=' + afterSequence + '&limit=200')
  if (!record(body) || !Array.isArray(body.items) || typeof body.has_more !== 'boolean' || !(body.active_run_id === null || typeof body.active_run_id === 'string')) return invalid()
  return { items: body.items.map(parseMessage), has_more: body.has_more, active_run_id: body.active_run_id }
}
export async function createRun(id: string, content: string, key: string): Promise<Snapshot> {
  return parseSnapshot(await requestJson('/api/conversations/' + encodeURIComponent(id) + '/runs', { method: 'POST', body: JSON.stringify({ content, idempotency_key: key }) }))
}
export async function getRun(id: string): Promise<Snapshot> { return parseSnapshot(await requestJson('/api/runs/' + encodeURIComponent(id))) }
export async function cancelRun(id: string): Promise<Snapshot> {
  return parseSnapshot(await requestJson('/api/runs/' + encodeURIComponent(id) + '/cancel', { method: 'POST' }))
}
export function watchRun(id: string, receive: (snapshot: Snapshot) => void, connection: (message: string) => void): () => void {
  const source = new EventSource(API_BASE_URL + '/api/runs/' + encodeURIComponent(id) + '/events?workspace_id=' + encodeURIComponent(getWorkspaceId()), { withCredentials: true })
  let disposed = false
  const handle = (event: MessageEvent<string>): void => {
    if (disposed) return
    try {
      const snapshot = parseSnapshot(JSON.parse(event.data) as unknown)
      connection('')
      receive(snapshot)
      if (!isActive(snapshot.run)) source.close()
    } catch { source.close(); connection('流式数据异常，请点击重新连接') }
  }
  for (const name of ['snapshot', 'completed', 'failed', 'cancelled']) source.addEventListener(name, handle as EventListener)
  source.addEventListener('access_revoked', () => { if (!disposed) { source.close(); notifyUnauthorized() } })
  source.onerror = () => { if (!disposed) { connection('连接中断，正在验证登录并重连'); void requestJson('/api/auth/me').catch(error => { if (error instanceof ApiError && error.status === 401) { source.close(); notifyUnauthorized() } }) } }
  return () => { disposed = true; source.close() }
}
