import { ApiError, requestJson } from './client'

export type AgentVisibility = 'workspace' | 'private'
export type AgentStatus = 'active' | 'archived'
export type SkillActivation = 'always' | 'on_demand'
export type KnowledgeMode = 'all_readable' | 'selected'

export interface ToolBinding {
  key: string
  display_name: string
  risk: string
  approval: string
  enabled: boolean
}

export interface SkillBinding {
  id: string
  key: string
  name: string
  version: number
  activation: SkillActivation
}

export interface AgentSkillInput {
  id: string
  activation: SkillActivation
}

export interface KnowledgeScope {
  mode: KnowledgeMode
  kb_ids: string[]
}

export interface Agent {
  id: string
  name: string
  description: string
  icon: string
  visibility: AgentVisibility
  status: AgentStatus
  version: number
  model: string
  owner_user_id: string
  is_owner: boolean
  can_manage: boolean
  instructions: string | null
  tools: ToolBinding[]
  skills: SkillBinding[]
  knowledge_scope: KnowledgeScope
  allow_planning: boolean
  allow_delegation: boolean
  starter_prompts: string[]
  created_at: string
  updated_at: string
}

export interface AgentInput {
  name: string
  description: string
  icon: string
  instructions: string
  visibility: AgentVisibility
  status: AgentStatus
  model: string
  allow_planning: boolean
  allow_delegation: boolean
  tool_keys: string[]
  skills: AgentSkillInput[]
  knowledge_mode: KnowledgeMode
  knowledge_base_ids: string[]
  starter_prompts: string[]
}

export interface AgentUpdate extends AgentInput {
  expected_version: number
}

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('服务返回的数据格式不正确', 502) }
function parseScope(value: unknown): KnowledgeScope {
  if (!record(value) || (value.mode !== 'all_readable' && value.mode !== 'selected')) return invalid()
  const ids = value.kb_ids
  if (!Array.isArray(ids) || !ids.every(item => typeof item === 'string')) return { mode: value.mode, kb_ids: [] }
  return { mode: value.mode, kb_ids: ids }
}
function parseTool(value: unknown): ToolBinding {
  if (!record(value) || typeof value.key !== 'string' || typeof value.display_name !== 'string'
    || typeof value.risk !== 'string' || typeof value.approval !== 'string' || typeof value.enabled !== 'boolean') return invalid()
  return value as unknown as ToolBinding
}
function parseSkill(value: unknown): SkillBinding {
  if (!record(value) || typeof value.id !== 'string' || typeof value.key !== 'string' || typeof value.name !== 'string'
    || typeof value.version !== 'number' || (value.activation !== 'always' && value.activation !== 'on_demand')) return invalid()
  return value as unknown as SkillBinding
}
export function parseAgent(value: unknown): Agent {
  if (!record(value) || typeof value.id !== 'string' || typeof value.name !== 'string' || typeof value.description !== 'string'
    || typeof value.icon !== 'string' || (value.visibility !== 'workspace' && value.visibility !== 'private')
    || (value.status !== 'active' && value.status !== 'archived') || typeof value.version !== 'number'
    || typeof value.model !== 'string' || typeof value.owner_user_id !== 'string'
    || typeof value.is_owner !== 'boolean' || typeof value.can_manage !== 'boolean'
    || !(value.instructions === null || typeof value.instructions === 'string')
    || !Array.isArray(value.tools) || !Array.isArray(value.skills)
    || typeof value.allow_planning !== 'boolean' || typeof value.allow_delegation !== 'boolean'
    || !Array.isArray(value.starter_prompts) || !value.starter_prompts.every(item => typeof item === 'string')
    || typeof value.created_at !== 'string' || typeof value.updated_at !== 'string') return invalid()
  return {
    ...value as unknown as Agent,
    tools: value.tools.map(parseTool),
    skills: value.skills.map(parseSkill),
    knowledge_scope: parseScope(value.knowledge_scope),
  }
}
export async function listAgents(offset = 0): Promise<Agent[]> {
  const body = await requestJson('/api/agents?offset=' + offset + '&limit=50')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseAgent)
}
export async function getAgent(id: string): Promise<Agent> {
  return parseAgent(await requestJson('/api/agents/' + encodeURIComponent(id)))
}
export async function createAgent(input: AgentInput): Promise<Agent> {
  return parseAgent(await requestJson('/api/agents', { method: 'POST', body: JSON.stringify(input) }))
}
export async function updateAgent(id: string, input: AgentUpdate): Promise<Agent> {
  return parseAgent(await requestJson('/api/agents/' + encodeURIComponent(id), { method: 'POST', body: JSON.stringify(input) }))
}
export async function archiveAgent(id: string): Promise<Agent> {
  return parseAgent(await requestJson('/api/agents/' + encodeURIComponent(id) + '/archive', { method: 'POST' }))
}
