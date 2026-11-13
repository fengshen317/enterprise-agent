import { ApiError, requestJson } from './client'

export type Role = 'admin' | 'member'
export type MemberStatus = 'active' | 'revoked'
export interface Member { user_id: string; username: string; display_name: string; user_status: string; role: Role; status: MemberStatus; joined_at: string; updated_at: string }
export interface MemberPage { items: Member[]; total: number; offset: number; limit: number }
export interface MemberCreate { username: string; display_name: string; password: string; role: Role }
export interface MemberUpdate { role: Role; status: MemberStatus }
export interface Audit { id: string; actor_username: string; target_username: string; action: string; before_role: string | null; before_status: string | null; after_role: string; after_status: string; created_at: string }
function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('管理接口返回格式不正确', 502) }
function member(value: unknown): Member {
  if (!record(value) || !['user_id', 'username', 'display_name', 'user_status', 'joined_at', 'updated_at'].every(key => typeof value[key] === 'string') || !['admin', 'member'].includes(String(value.role)) || !['active', 'revoked'].includes(String(value.status))) return invalid()
  return value as unknown as Member
}
export async function listMembers(offset = 0, query = ''): Promise<MemberPage> {
  const body = await requestJson('/api/admin/members?limit=20&offset=' + offset + '&q=' + encodeURIComponent(query))
  if (!record(body) || !Array.isArray(body.items) || typeof body.total !== 'number' || typeof body.offset !== 'number' || typeof body.limit !== 'number') return invalid()
  return { items: body.items.map(member), total: body.total, offset: body.offset, limit: body.limit }
}
export async function createMember(body: MemberCreate): Promise<Member> {
  return member(await requestJson('/api/admin/members', { method: 'POST', body: JSON.stringify(body) }))
}
export async function updateMember(id: string, body: MemberUpdate): Promise<Member> {
  return member(await requestJson('/api/admin/members/' + encodeURIComponent(id), { method: 'POST', body: JSON.stringify(body) }))
}
export async function listAudits(offset = 0): Promise<Audit[]> {
  const body = await requestJson('/api/admin/audits?limit=20&offset=' + offset)
  if (!Array.isArray(body) || !body.every((row: unknown) => record(row) && ['id', 'actor_username', 'target_username', 'action', 'after_role', 'after_status', 'created_at'].every(key => typeof row[key] === 'string') && (row.before_role === null || typeof row.before_role === 'string') && (row.before_status === null || typeof row.before_status === 'string'))) return invalid()
  return body as Audit[]
}
