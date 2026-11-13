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

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null }
function invalid(): never { throw new ApiError('配置接口返回格式不正确', 502) }
function parseItem(value: unknown): ConfigItem {
  if (!record(value) || typeof value.key !== 'string' || typeof value.value_masked !== 'string'
    || typeof value.is_secret !== 'boolean' || typeof value.description !== 'string'
    || !(value.updated_at === null || typeof value.updated_at === 'string')
    || !(value.updated_by === null || typeof value.updated_by === 'string')) return invalid()
  return value as unknown as ConfigItem
}

export async function listConfigs(): Promise<ConfigItem[]> {
  const body = await requestJson('/api/admin/configs')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseItem)
}

export async function updateConfigs(configs: ConfigUpdate[]): Promise<ConfigItem[]> {
  const body = await requestJson('/api/admin/configs', {
    method: 'POST',
    body: JSON.stringify({ configs }),
  })
  if (!Array.isArray(body)) return invalid()
  return body.map(parseItem)
}
