import { ApiError, requestJson } from './client'

const settingsPath = '/api/admin/knowledge-model'
const modelSetupTimeout = 90_000

export type EmbeddingPreset = 'siliconflow_bge_m3' | 'openai_small' | 'compatible'

export interface ActiveEmbedding {
  provider: string
  name: string
  instance: string
}

export interface EmbeddingStatus {
  state: 'not_connected' | 'needs_model' | 'ready'
  model: ActiveEmbedding | null
  environment_override: string
}

export interface EmbeddingSetup {
  preset: EmbeddingPreset
  api_key: string
  base_url?: string
  model_name?: string
  dimension?: number
  batch_size?: number
  max_tokens?: number
}

function parseStatus(value: unknown): EmbeddingStatus {
  if (typeof value !== 'object' || value === null) throw new ApiError('知识库模型响应格式不正确', 502)
  const status = value as Partial<EmbeddingStatus>
  if (status.state !== 'not_connected' && status.state !== 'needs_model' && status.state !== 'ready') {
    throw new ApiError('知识库模型状态不正确', 502)
  }
  if (typeof status.environment_override !== 'string'
    || (status.model !== null && (
      !status.model || typeof status.model.provider !== 'string'
      || typeof status.model.name !== 'string' || typeof status.model.instance !== 'string'
    ))) {
    throw new ApiError('知识库模型信息不正确', 502)
  }
  return status as EmbeddingStatus
}

export async function getEmbeddingStatus(): Promise<EmbeddingStatus> {
  return parseStatus(await requestJson(settingsPath))
}

export async function saveEmbedding(setup: EmbeddingSetup): Promise<EmbeddingStatus> {
  return parseStatus(await requestJson(
    settingsPath,
    { method: 'POST', body: JSON.stringify(setup) },
    modelSetupTimeout,
  ))
}
