import { afterEach, expect, it, vi } from 'vitest'
import { setSecurityContext } from './client'
import { getSystemLog, listSystemLogs } from './systemLogs'

const event = {
  id: '41361f62-a7c2-49e3-bf1e-70ffc177cb28',
  occurred_at: '2026-11-19T11:24:17.306+08:00', level: 'error', component: 'tool',
  event: 'tool_finished', outcome: 'error', error_code: 'CONNECTOR_INVALID_OUTPUT',
  details: { upstream_status: 200, upstream_code: 0, raw_response: 'private payload' },
  arguments: { secret: 'private payload' },
}

afterEach(() => {
  setSecurityContext('', '')
  vi.unstubAllGlobals()
})

it('发送当前空间和编码后的筛选项，只读取已定义的诊断字段', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    items: [event], total: 1, counts: { all: 4, error: 1, warning: 1, success: 2 }, next_cursor: null,
  })))
  vi.stubGlobal('fetch', fetch)
  setSecurityContext('csrf', 'workspace')

  const page = await listSystemLogs({ days: 7, limit: 50, component: 'tool', level: 'error', outcome: 'error', q: event.id, cursor: 'a/+=' })
  const [url, init] = fetch.mock.calls[0]!
  expect(new URL(url as string, 'http://localhost').searchParams.get('cursor')).toBe('a/+=')
  expect(new URL(url as string, 'http://localhost').searchParams.get('q')).toBe(event.id)
  expect(init).toMatchObject({ headers: { 'X-Workspace-ID': 'workspace' } })
  expect(page.counts.all).toBe(4)
  expect(page.items[0]?.details).toEqual({ upstream_status: 200, upstream_code: 0 })
  expect(page.items[0]).not.toHaveProperty('arguments')
  expect(page.items[0]?.duration_ms).toBeNull()
})

it('读取单条详情及关联记录，拒绝错误的响应格式', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ event, related: [event], related_truncated: false })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ event, related: [] })))
  vi.stubGlobal('fetch', fetch)
  const result = await getSystemLog(event.id)
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/admin/system-logs/' + event.id), expect.any(Object))
  expect(result.related[0]?.id).toBe(event.id)
  await expect(getSystemLog(event.id)).rejects.toMatchObject({ status: 502 })
})
