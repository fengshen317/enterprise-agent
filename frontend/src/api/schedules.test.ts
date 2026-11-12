import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSchedule, deleteSchedule, listSchedules, runScheduleNow, updateSchedule, type Schedule } from './schedules'

const schedule: Schedule = {
  id: 's1',
  conversation_id: 'c1',
  conversation_title: '每日工作',
  name: '晨报',
  prompt: '总结今天的重点',
  cron_expression: '0 9 * * *',
  timezone: 'Asia/Shanghai',
  enabled: true,
  next_run_at: '2026-11-12T01:00:00Z',
  last_run_at: null,
  last_run_id: null,
  last_status: null,
  last_error_code: null,
  created_at: '2026-11-11T00:00:00Z',
  updated_at: '2026-11-11T00:00:00Z',
}

afterEach(() => vi.unstubAllGlobals())

describe('定时任务接口', () => {
  it('列表与创建会校验返回结构并携带完整输入', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([schedule]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(schedule), { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    expect(await listSchedules()).toEqual([schedule])
    const input = { name: '晨报', prompt: '总结', conversation_id: 'c1', cron_expression: '0 9 * * *', timezone: 'Asia/Shanghai', enabled: true }
    expect(await createSchedule(input)).toEqual(schedule)
    expect(JSON.parse(fetcher.mock.calls[1]?.[1]?.body as string)).toEqual(input)
  })

  it('更新、删除与立即执行复用现有运行快照解析', async () => {
    const snapshot = { run: { id: 'r1', conversation_id: 'c1', status: 'pending', model: 'test', error_code: null, error_message: null }, message: { id: 'm1', conversation_id: 'c1', run_id: 'r1', sequence: 2, role: 'assistant', content: '', status: 'streaming' } }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...schedule, enabled: false }), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(snapshot), { status: 202 }))
    vi.stubGlobal('fetch', fetcher)
    const input = { name: '晨报', prompt: '总结', conversation_id: 'c1', cron_expression: '0 9 * * *', timezone: 'Asia/Shanghai', enabled: false }
    expect((await updateSchedule('s1', input)).enabled).toBe(false)
    await deleteSchedule('s1')
    expect(await runScheduleNow('s1')).toEqual(snapshot)
    expect(fetcher.mock.calls.map(call => String(call[0]))).toEqual(['/api/schedules/s1', '/api/schedules/s1/delete', '/api/schedules/s1/run'])
  })

  it('非法响应不会被当作成功', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([{ ...schedule, enabled: 'yes' }]), { status: 200 })))
    await expect(listSchedules()).rejects.toThrow('格式不正确')
  })
})
