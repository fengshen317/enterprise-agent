import { afterEach, describe, expect, it, vi } from 'vitest'
import { getHealth } from './health'

afterEach(() => { vi.unstubAllGlobals() })

describe('健康接口契约', () => {
  it('读取就绪响应', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ready', service: 'api', version: '0.1.0' }))))
    await expect(getHealth('ready')).resolves.toEqual({ status: 'ready', service: 'api', version: '0.1.0' })
  })
  it('拒绝状态语义不匹配的成功响应', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok', service: 'api', version: '0.1.0' }))))
    await expect(getHealth('ready')).rejects.toMatchObject({ status: 502 })
  })
  it('保留后端公开错误与请求标识', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: '数据库未就绪' } }), { status: 503, headers: { 'X-Request-ID': 'test-id' } })))
    await expect(getHealth('ready')).rejects.toMatchObject({ status: 503, message: '数据库未就绪', requestId: 'test-id' })
  })
  it('网络异常不回显内部细节', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('secret-internal-detail')))
    await expect(getHealth('live')).rejects.toMatchObject({ status: 0, message: '无法连接服务，请稍后重试' })
  })
})
