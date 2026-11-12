import { ApiError, requestJson } from './client'
import type { KnowledgeMode, SkillActivation } from './agents'

export type SkillVisibility = 'workspace' | 'private'
export type SkillStatus = 'draft' | 'published' | 'archived'

export interface Skill {
  id: string
  key: string
  name: string
  description: string
  visibility: SkillVisibility
  status: SkillStatus
  current_version: number
  owner_user_id: string
  is_owner: boolean
  can_manage: boolean
  activation: SkillActivation
  tool_keys: string[]
  knowledge_scope: { mode: KnowledgeMode; kb_ids: string[] }
  token_budget: number
  content_md: string | null
  created_at: string
  updated_at: string
}

export interface SkillInput {
  key: string
  name: string
  description: string
  visibility: SkillVisibility
  content_md: string
  tool_keys: string[]
  knowledge_mode: KnowledgeMode
  knowledge_base_ids: string[]
  activation: SkillActivation
  token_budget: number
  change_note: string
}

export interface SkillVersion {
  id: string
  version: number
  content_md: string
  content_sha256: string
  tool_keys: string[]
  knowledge_scope: { mode: KnowledgeMode; kb_ids: string[] }
  activation: SkillActivation
  token_budget: number
  change_note: string
  created_at: string
  published_at: string | null
}

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('服务返回的数据格式不正确', 502) }
function parseScope(value: unknown): { mode: KnowledgeMode; kb_ids: string[] } {
  if (!record(value) || (value.mode !== 'all_readable' && value.mode !== 'selected')) return invalid()
  const ids = value.kb_ids
  return { mode: value.mode, kb_ids: Array.isArray(ids) ? ids.filter((item): item is string => typeof item === 'string') : [] }
}
export function parseSkill(value: unknown): Skill {
  if (!record(value) || typeof value.id !== 'string' || typeof value.key !== 'string' || typeof value.name !== 'string'
    || typeof value.description !== 'string' || (value.visibility !== 'workspace' && value.visibility !== 'private')
    || !['draft', 'published', 'archived'].includes(String(value.status))
    || typeof value.current_version !== 'number' || typeof value.owner_user_id !== 'string'
    || typeof value.is_owner !== 'boolean' || typeof value.can_manage !== 'boolean'
    || (value.activation !== 'always' && value.activation !== 'on_demand')
    || !Array.isArray(value.tool_keys) || !value.tool_keys.every(item => typeof item === 'string')
    || typeof value.token_budget !== 'number' || !(value.content_md === null || typeof value.content_md === 'string')
    || typeof value.created_at !== 'string' || typeof value.updated_at !== 'string') return invalid()
  return {
    ...value as unknown as Skill,
    knowledge_scope: parseScope(value.knowledge_scope),
  }
}
function parseVersion(value: unknown): SkillVersion {
  if (!record(value) || typeof value.id !== 'string' || typeof value.version !== 'number'
    || typeof value.content_md !== 'string' || typeof value.content_sha256 !== 'string'
    || !Array.isArray(value.tool_keys) || !value.tool_keys.every(item => typeof item === 'string')
    || (value.activation !== 'always' && value.activation !== 'on_demand')
    || typeof value.token_budget !== 'number' || typeof value.change_note !== 'string'
    || typeof value.created_at !== 'string' || !(value.published_at === null || typeof value.published_at === 'string')) return invalid()
  return { ...value as unknown as SkillVersion, knowledge_scope: parseScope(value.knowledge_scope) }
}
export async function listSkills(offset = 0): Promise<Skill[]> {
  const body = await requestJson('/api/skills?offset=' + offset + '&limit=50')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseSkill)
}
export async function getSkill(id: string): Promise<Skill> {
  return parseSkill(await requestJson('/api/skills/' + encodeURIComponent(id)))
}
export async function createSkill(input: SkillInput): Promise<Skill> {
  return parseSkill(await requestJson('/api/skills', { method: 'POST', body: JSON.stringify(input) }))
}
export async function updateSkill(id: string, input: SkillInput): Promise<Skill> {
  return parseSkill(await requestJson('/api/skills/' + encodeURIComponent(id), { method: 'POST', body: JSON.stringify(input) }))
}
export async function publishSkill(id: string, version: number): Promise<Skill> {
  return parseSkill(await requestJson('/api/skills/' + encodeURIComponent(id) + '/publish', { method: 'POST', body: JSON.stringify({ version }) }))
}
export async function archiveSkill(id: string): Promise<Skill> {
  return parseSkill(await requestJson('/api/skills/' + encodeURIComponent(id) + '/archive', { method: 'POST' }))
}
export async function listSkillVersions(id: string): Promise<SkillVersion[]> {
  const body = await requestJson('/api/skills/' + encodeURIComponent(id) + '/versions')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseVersion)
}
