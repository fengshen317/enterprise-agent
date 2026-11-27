import { beforeEach, expect, it, vi } from 'vitest'
import { requestJson } from './client'
import { authenticateConnector, checkImaAccount, getConnectorAuthStatus, listRssSources, testConnector } from './connectors'

vi.mock('./client', async original => ({
  ...await original<typeof import('./client')>(), requestJson: vi.fn(),
}))

beforeEach(() => { vi.mocked(requestJson).mockReset() })

it('预置信源读取接口校验目录内容', async () => {
  const sources = [{ name: 'OpenAI', category: '官方动态', url: 'https://openai.com/news/rss.xml' }]
  vi.mocked(requestJson).mockResolvedValueOnce(sources).mockResolvedValueOnce([{ ...sources[0], url: 'javascript:x' }])
  await expect(listRssSources()).resolves.toEqual(sources)
  expect(requestJson).toHaveBeenCalledWith('/api/connectors/rss/sources')
  await expect(listRssSources()).rejects.toThrow('连接器数据格式不正确')
})

it('允许带有说明的待配置信源，拒绝无法识别的空地址', async () => {
  const pending = [{ name: 'X', category: '社交平台', url: '', description: '待配置 RSSHub' }]
  vi.mocked(requestJson).mockResolvedValueOnce(pending).mockResolvedValueOnce([{ ...pending[0], description: '' }])
  await expect(listRssSources()).resolves.toEqual(pending)
  await expect(listRssSources()).rejects.toThrow('连接器数据格式不正确')
})

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
  expect(requestJson).toHaveBeenCalledWith('/api/connectors/cli%2Fname/my-account/auth', { method: 'POST' }, 90_000)
  await getConnectorAuthStatus('cli/name')
  expect(requestJson).toHaveBeenLastCalledWith('/api/connectors/cli%2Fname/my-account/auth/status')
  await getConnectorAuthStatus('yangjibao')
  expect(requestJson).toHaveBeenLastCalledWith('/api/connectors/yangjibao/my-account/auth/status', { method: 'POST' }, 12_000)
})
