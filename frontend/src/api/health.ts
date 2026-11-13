import { ApiError, getJson } from './client'

export interface HealthResult {
  status: 'ok' | 'ready'
  service: string
  version: string
}

export async function getHealth(kind: 'live' | 'ready'): Promise<HealthResult> {
  const body = await getJson('/api/health/' + kind)
  const expected = kind === 'live' ? 'ok' : 'ready'
  if (typeof body !== 'object' || body === null || !('status' in body) || body.status !== expected ||
    !('service' in body) || typeof body.service !== 'string' || !('version' in body) || typeof body.version !== 'string') {
    throw new ApiError('健康检查响应格式不正确', 502)
  }
  return { status: expected, service: body.service, version: body.version }
}
