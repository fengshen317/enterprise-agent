import { expect, it, vi } from 'vitest'
import { parseSnapshot } from './chat'
import { getChatReport } from './chatReports'
import { requestJson } from './client'

vi.mock('./client', async (getActual) => ({ ...await getActual<typeof import('./client')>(), requestJson: vi.fn() }))

it('运行快照可携带报表引用并兼容旧消息', () => {
  const message = { id: 'm', conversation_id: 'c', run_id: 'r', sequence: 2, role: 'assistant', content: '', status: 'completed' }
  const run = { id: 'r', conversation_id: 'c', status: 'completed', model: 'test', error_code: null, error_message: null }
  expect(parseSnapshot({ message, run }).message).toEqual(message)
  expect(parseSnapshot({ run, message: { ...message, reports: [{ id: 'a', title: '月报', code: '忽略附加字段' }] } })
    .message.reports).toEqual([{ id: 'a', title: '月报' }])
  expect(() => parseSnapshot({ run, message: { ...message, reports: [{ id: 'a', title: null }] } })).toThrow()
})

it('请求会话报表时校验引用及用于绘图的列名', async () => {
  const chart = { title: '月度销量', type: 'bar', x: '月份', y: ['销量'], unit: '件' }
  const report = {
    id: 'a/b', title: '月报', explanation: '按月合计', columns: ['月份', '销量'],
    rows: [['一月', 7]], charts: [chart], source_files: ['销量.csv'], code: 'print(total)',
  }
  const request = vi.mocked(requestJson)
  request.mockResolvedValueOnce(report)
  await expect(getChatReport('c/d', 'a/b')).resolves.toEqual(report)
  expect(request).toHaveBeenCalledWith('/api/conversations/c%2Fd/reports/a%2Fb')

  request.mockResolvedValueOnce({ ...report, id: 'another-report' })
    .mockResolvedValueOnce({ ...report, rows: [['一月', { data: 7 }]] })
    .mockResolvedValueOnce({ ...report, charts: [{ ...chart, type: 'pie' }] })
    .mockResolvedValueOnce({ ...report, charts: [{ ...chart, y: ['未知列'] }] })
  for (let index = 0; index < 4; index++) {
    await expect(getChatReport('c/d', 'a/b')).rejects.toMatchObject({ status: 502 })
  }
})
