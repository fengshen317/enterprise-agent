import { beforeEach, expect, it, vi } from 'vitest'
import { requestJson } from './client'
import { authenticateConnector, checkImaAccount, getConnectorAuthStatus, testConnector } from './connectors'

vi.mock('./client', async original => ({
  ...await original<typeof import('./client')>(), requestJson: vi.fn(),
}))

beforeEach(() => { vi.mocked(requestJson).mockReset() })

it('连接检查保留后端最大执行时间和传输余量', async () => {
  vi.mocked(requestJson).mockResolvedValue({ status: 'connected' })
  await testConnector('cli/name')
  expect(requestJson).toHaveBeenCalledWith('/api/admin/connectors/cli%2Fname/test', { method: 'POST' }, 65_000)
})

it('个人 ima 检查按两项返回，且请求只使用已保存凭证', async () => {
  const result = {
    knowledge_base: { status: 'success', error_code: null, diagnostic_id: null },
    notes: { status: 'error', error_code: 'auth_failed', diagnostic_id: null },
  }
  vi.mocked(requestJson).mockResolvedValueOnce(result).mockResolvedValueOnce({ knowledge_base: result.knowledge_base })
  await expect(checkImaAccount()).resolves.toEqual(result)
  expect(requestJson).toHaveBeenCalledWith('/api/connectors/ima/my-account/check', { method: 'POST', cache: 'no-store' }, 20_000)
  await expect(checkImaAccount()).rejects.toThrow('连接器数据格式不正确')
})

it('授权启动允许健康检查和地址生成完成，状态查询采用默认超时', async () => {
  vi.mocked(requestJson).mockResolvedValue({ state: 'awaiting_user' })
  await authenticateConnector('cli/name')
  expect(requestJson).toHaveBeenCalledWith('/api/admin/connectors/cli%2Fname/auth', { method: 'POST' }, 90_000)
  await getConnectorAuthStatus('cli/name')
  expect(requestJson).toHaveBeenLastCalledWith('/api/admin/connectors/cli%2Fname/auth/status')
  await getConnectorAuthStatus('yangjibao')
  expect(requestJson).toHaveBeenLastCalledWith('/api/admin/connectors/yangjibao/auth/status', { method: 'POST' }, 12_000)
})
