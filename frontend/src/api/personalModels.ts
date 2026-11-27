import { ApiError, requestJson } from './client'
import { parseProfile, parseChecks, type ModelProfile, type ModelProfileDraft, type ModelProviderPreset } from './config'

export interface SharedModel { name: string; provider: 'deepseek' | 'openai_compatible'; model_id: string; vision_model_id: string; base_url: string }
export interface PersonalModels { profiles: ModelProfile[]; shared_default: SharedModel }
const BASE = '/api/me/models'
const CATALOG_TIMEOUT_MS = 15000
const TEST_TIMEOUT_MS = 35000

function parse(value: unknown): PersonalModels {
  if (typeof value !== 'object' || value === null || !('profiles' in value) || !Array.isArray(value.profiles)
    || !('shared_default' in value) || typeof value.shared_default !== 'object' || value.shared_default === null) {
    throw new ApiError('个人模型响应格式不正确', 502)
  }
  const shared = value.shared_default as Record<string, unknown>
  if (typeof shared.name !== 'string' || typeof shared.model_id !== 'string' || typeof shared.base_url !== 'string'
    || typeof shared.vision_model_id !== 'string' || !['deepseek', 'openai_compatible'].includes(String(shared.provider))) {
    throw new ApiError('公共模型响应格式不正确', 502)
  }
  return { profiles: value.profiles.map(parseProfile), shared_default: shared as unknown as SharedModel }
}
export async function listPersonalModels(): Promise<PersonalModels> { return parse(await requestJson(BASE)) }
export async function addPersonalModel(draft: ModelProfileDraft, makeDefault: boolean): Promise<PersonalModels> {
  return parse(await requestJson(BASE, { method: 'POST', body: JSON.stringify({ ...draft, make_default: makeDefault }) }))
}
export async function activatePersonalModel(id: string): Promise<PersonalModels> {
  return parse(await requestJson(`${BASE}/${encodeURIComponent(id)}/activate`, { method: 'POST' }))
}
export async function followSharedModel(): Promise<PersonalModels> {
  return parse(await requestJson(`${BASE}/follow-shared`, { method: 'POST' }))
}
export async function personalProviders(): Promise<ModelProviderPreset[]> {
  const body: unknown = await requestJson(`${BASE}/providers`)
  if (!Array.isArray(body) || !body.every(item => typeof item === 'object' && item !== null
    && typeof item.id === 'string' && typeof item.label === 'string' && typeof item.base_url === 'string'
    && typeof item.docs_url === 'string' && typeof item.supports_discovery === 'boolean')) {
    throw new ApiError('厂商列表响应格式不正确', 502)
  }
  return body as ModelProviderPreset[]
}
export async function discoverPersonalModels(provider: string, key: string): Promise<string[]> {
  const body: unknown = await requestJson(`${BASE}/providers/${encodeURIComponent(provider)}/models`, {
    method: 'POST', body: JSON.stringify({ api_key: key }),
  }, CATALOG_TIMEOUT_MS)
  if (!Array.isArray(body) || !body.every(item => typeof item === 'string')) throw new ApiError('模型列表格式不正确', 502)
  return body as string[]
}
export async function testPersonalModel(draft: ModelProfileDraft) {
  return parseChecks(await requestJson(`${BASE}/test`, { method: 'POST', body: JSON.stringify(draft) }, TEST_TIMEOUT_MS))
}
