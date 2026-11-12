import { ApiError, requestJson } from './client'

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired' | 'cancelled' | 'executed' | 'failed'
export type ApprovalRisk = 'write' | 'delete' | 'external'

export interface Approval {
  id: string
  run_id: string
  tool_call_id: string
  tool_name: string
  tool_version: number
  risk_level: ApprovalRisk
  args_preview: Record<string, unknown>
  status: ApprovalStatus
  requested_at: string
  expires_at: string
  decided_at: string | null
  decision_comment: string | null
}

export interface RunTrace {
  run_id: string
  agent_snapshot: Record<string, unknown>
  tool_calls: Record<string, unknown>[]
  approvals: Approval[]
}

const STATUSES: ApprovalStatus[] = ['pending', 'approved', 'rejected', 'expired', 'cancelled', 'executed', 'failed']
const RISKS: ApprovalRisk[] = ['write', 'delete', 'external']

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('服务返回的数据格式不正确', 502) }

export function parseApproval(value: unknown): Approval {
  if (!record(value) || typeof value.id !== 'string' || typeof value.run_id !== 'string' || typeof value.tool_call_id !== 'string'
    || typeof value.tool_name !== 'string' || typeof value.tool_version !== 'number' || !RISKS.includes(value.risk_level as ApprovalRisk)
    || !record(value.args_preview) || !STATUSES.includes(value.status as ApprovalStatus)
    || typeof value.requested_at !== 'string' || typeof value.expires_at !== 'string'
    || !(value.decided_at === null || typeof value.decided_at === 'string')
    || !(value.decision_comment === null || typeof value.decision_comment === 'string')) return invalid()
  return value as unknown as Approval
}

export async function listApprovals(runId: string): Promise<Approval[]> {
  const body = await requestJson('/api/runs/' + encodeURIComponent(runId) + '/approvals')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseApproval)
}

export async function decideApproval(runId: string, approvalId: string, decision: 'approve' | 'reject', comment?: string): Promise<Approval> {
  return parseApproval(await requestJson(
    '/api/runs/' + encodeURIComponent(runId) + '/approvals/' + encodeURIComponent(approvalId) + '/decide',
    { method: 'POST', body: JSON.stringify({ decision, comment: comment ?? null }) },
  ))
}

export async function getRunTrace(runId: string): Promise<RunTrace> {
  const body = await requestJson('/api/runs/' + encodeURIComponent(runId) + '/trace')
  if (!record(body) || typeof body.run_id !== 'string' || !record(body.agent_snapshot)
    || !Array.isArray(body.tool_calls) || !body.tool_calls.every(record) || !Array.isArray(body.approvals)) return invalid()
  return { run_id: body.run_id, agent_snapshot: body.agent_snapshot, tool_calls: body.tool_calls, approvals: body.approvals.map(parseApproval) }
}
