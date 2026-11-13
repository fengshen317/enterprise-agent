import { afterEach, describe, expect, it, vi } from 'vitest'
import { decideApproval, parseApproval } from './approvals'

const raw = {
  id: 'ap1', run_id: 'r1', tool_call_id: 't1', tool_name: 'create_scheduled_task', tool_version: 1,
  risk_level: 'write', args_preview: { summary: '创建定时任务' }, status: 'pending',
  requested_at: '2026-11-12T00:00:00Z', expires_at: '2026-11-12T00:05:00Z', decided_at: null, decision_comment: null,
}

afterEach(() => vi.unstubAllGlobals())
describe('审批接口', () => {
  it('拒绝非法状态与非对象预览', () => {
    expect(parseApproval(raw).risk_level).toBe('write')
    expect(() => parseApproval({ ...raw, status: 'waiting' })).toThrow('数据格式')
    expect(() => parseApproval({ ...raw, args_preview: 'x' })).toThrow('数据格式')
  })
  it('提交批准决定并解析结果', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...raw, status: 'approved', decided_at: '2026-11-12T00:01:00Z' }), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    expect((await decideApproval('r1', 'ap1', 'approve', '确认')).status).toBe('approved')
    expect(String(fetcher.mock.calls[0]?.[0])).toContain('/api/runs/r1/approvals/ap1/decide')
    expect(JSON.parse(fetcher.mock.calls[0]?.[1]?.body as string)).toEqual({ decision: 'approve', comment: '确认' })
  })
})
