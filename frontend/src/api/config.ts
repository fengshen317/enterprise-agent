import { ApiError, requestJson } from './client'

export interface ConfigItem {
  key: string
  value_masked: string
  is_secret: boolean
  description: string
  updated_at: string | null
  updated_by: string | null
}

export interface ConfigUpdate {
  key: string
  value: string
}

export type ConfigGroup = 'llm' | 'search'
export type ModelProvider = 'deepseek' | 'openai_compatible'

export interface ConfigCheck {
  check: 'text' | 'vision' | 'search'
  ok: boolean
  message: string
}

interface ModelProfileFields {
  name: string
  provider: ModelProvider
  base_url: string
  model_id: string
  vision_model_id: string
}

export interface ModelProfileDraft extends ModelProfileFields {
  api_key: string
}

export interface ModelProfile extends ModelProfileFields {
  id: string
  active: boolean
  has_api_key: boolean
}

// 文本和图文分别发起请求，每次最多等待 15 秒。
const CONFIG_TEST_TIMEOUT_MS = 35000

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('配置接口返回格式不正确', 502) }
function parseItem(value: unknown): ConfigItem {
  if (!record(value) || typeof value.key !== 'string' || typeof value.value_masked !== 'string'
    || typeof value.is_secret !== 'boolean' || typeof value.description !== 'string'
    || !(value.updated_at === null || typeof value.updated_at === 'string')
    || !(value.updated_by === null || typeof value.updated_by === 'string')) return invalid()
  return value as unknown as ConfigItem
}

function parseConfigList(body: unknown): ConfigItem[] {
  if (!Array.isArray(body)) return invalid()
  return body.map(parseItem)
}

function parseProfile(item: unknown): ModelProfile {
  if (!record(item) || typeof item.id !== 'string' || typeof item.name !== 'string'
    || (item.provider !== 'deepseek' && item.provider !== 'openai_compatible')
    || typeof item.base_url !== 'string' || typeof item.model_id !== 'string' || typeof item.vision_model_id !== 'string'
    || typeof item.active !== 'boolean' || typeof item.has_api_key !== 'boolean') return invalid()
  return item as unknown as ModelProfile
}

export async function listConfigs(): Promise<ConfigItem[]> {
  return parseConfigList(await requestJson('/api/admin/configs'))
}

export async function updateConfigs(configs: ConfigUpdate[]): Promise<ConfigItem[]> {
  const body = await requestJson('/api/admin/configs', {
    method: 'POST',
    body: JSON.stringify({ configs }),
  })
  return parseConfigList(body)
}

function parseChecks(body: unknown): ConfigCheck[] {
  if (!Array.isArray(body) || body.length === 0) return invalid()
  return body.map((item: unknown) => {
    if (!record(item) || (item.check !== 'text' && item.check !== 'vision' && item.check !== 'search')
      || typeof item.ok !== 'boolean' || typeof item.message !== 'string') return invalid()
    return { check: item.check, ok: item.ok, message: item.message }
  })
}

export async function testConfigs(group: ConfigGroup, configs: ConfigUpdate[]): Promise<ConfigCheck[]> {
  return parseChecks(await requestJson('/api/admin/configs/test', {
    method: 'POST', body: JSON.stringify({ group, configs }),
  }, CONFIG_TEST_TIMEOUT_MS))
}

export async function listModelProfiles(): Promise<ModelProfile[]> {
  const body = await requestJson('/api/admin/model-profiles')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseProfile)
}

export async function addModelProfile(profile: ModelProfileDraft): Promise<ModelProfile> {
  return parseProfile(await requestJson('/api/admin/model-profiles', {
    method: 'POST', body: JSON.stringify(profile),
  }))
}

export async function testModelProfile(profile: ModelProfileDraft): Promise<ConfigCheck[]> {
  return parseChecks(await requestJson('/api/admin/model-profiles/test', {
    method: 'POST', body: JSON.stringify(profile),
  }, CONFIG_TEST_TIMEOUT_MS))
}

export async function activateModelProfile(id: string): Promise<ConfigItem[]> {
  return parseConfigList(await requestJson(`/api/admin/model-profiles/${encodeURIComponent(id)}/activate`, {
    method: 'POST',
  }))
}
