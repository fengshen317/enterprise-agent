import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { confirmTask, controlTask, parseTask, updateTaskPlan, watchTask, type TaskSnapshot } from './tasks'
import { requestJson, setSecurityContext } from './client'

vi.mock('./client', async () => ({ ...await vi.importActual<typeof import('./client')>('./client'), requestJson: vi.fn() }))
const snapshot: TaskSnapshot = { task: { id: 't1', goal: '测试目标', workspace_id: 'w1', owner_user_id: 'u1', created_at: '' }, run: { id: 'r1', status: 'running', error_code: null, result: null, deadline_at: null, next_retry_at: null, fencing_token: 1, lease_owner: 'local', version: 2 }, plan: null, steps: [], actions: [], last_event_sequence: 3 }
class FakeSource extends EventTarget {
  static instances: FakeSource[] = []
  closed = false
  onerror: (() => void) | null = null
  constructor(public url: string, public options?: EventSourceInit) { super(); FakeSource.instances.push(this) }
  close(): void { this.closed = true }
}
beforeEach(() => {
  vi.resetAllMocks(); FakeSource.instances = []
  vi.stubGlobal('EventSource', FakeSource)
  setSecurityContext('csrf', 'w1')
  vi.mocked(requestJson).mockResolvedValue(snapshot)
})
afterEach(() => { vi.unstubAllGlobals(); setSecurityContext('', '') })
it('确认与控制绑定当前执行和版本，不修改其他执行', async () => {
  await confirmTask('t1', 2, 'digest', 'r1')
  expect(requestJson).toHaveBeenLastCalledWith('/api/tasks/t1/confirm', { method: 'POST', body: JSON.stringify({ revision: 2, digest: 'digest', run_id: 'r1' }) })
  await controlTask('t1', 'cancel', 'r1', 5)
  expect(requestJson).toHaveBeenLastCalledWith('/api/tasks/t1/cancel', { method: 'POST', body: JSON.stringify({ run_id: 'r1', version: 5 }) })
  await updateTaskPlan('t1', { steps: [], run_id: 'spoof', revision: 99 }, 'r1', 2)
  expect(requestJson).toHaveBeenLastCalledWith('/api/tasks/t1/plan', { method: 'POST', body: JSON.stringify({ steps: [], run_id: 'r1', revision: 2 }) })
})
it('SSE 使用公开空间和游标，接收终态后关闭', () => {
  const receive = vi.fn(); const connection = vi.fn()
  const dispose = watchTask('t1', 3, receive, connection)
  const source = FakeSource.instances[0]!
  expect(source.url).toContain('workspace_id=w1&after_sequence=3')
  expect(source.options).toEqual({ withCredentials: true })
  source.dispatchEvent(new MessageEvent('snapshot', { data: JSON.stringify(snapshot) }))
  expect(receive).toHaveBeenCalledWith(snapshot)
  expect(connection).toHaveBeenCalledWith('')
  source.dispatchEvent(new MessageEvent('snapshot', { data: JSON.stringify({ ...snapshot, run: { ...snapshot.run, status: 'completed' } }) }))
  expect(source.closed).toBe(true)
  dispose()
})
it('订阅销毁后迟到事件与断线都不继续更新页面', () => {
  const receive = vi.fn(); const connection = vi.fn()
  const dispose = watchTask('t1', 0, receive, connection)
  const source = FakeSource.instances[0]!
  dispose()
  source.dispatchEvent(new MessageEvent('snapshot', { data: JSON.stringify(snapshot) }))
  source.onerror?.()
  expect(receive).not.toHaveBeenCalled(); expect(connection).not.toHaveBeenCalled()
})
it('无效或跨任务的SSE数据被关闭并提示重新读取', () => {
  const receive = vi.fn(); const connection = vi.fn()
  watchTask('t1', 0, receive, connection)
  const source = FakeSource.instances[0]!
  source.dispatchEvent(new MessageEvent('snapshot', { data: JSON.stringify({ ...snapshot, task: { ...snapshot.task, id: 't2' } }) }))
  expect(source.closed).toBe(true)
  expect(connection).toHaveBeenCalledWith('进度数据异常，请重新读取')
  expect(receive).not.toHaveBeenCalled()
  expect(() => parseTask({ ...snapshot, steps: [{ status: 'success' }] })).toThrow('任务响应格式不正确')
})

it('请求尝试摘要字段错误不能伪装成合法进度', () => {
  expect(() => parseTask({ ...snapshot, actions: [{ id: 'a1', step_key: null, kind: 'model', status: 'retry_wait', attempt_count: '2', max_attempts: 3, error_code: null, next_retry_at: null }] })).toThrow('任务响应格式不正确')
  expect(() => parseTask({ ...snapshot, actions: [{ id: 'a1', step_key: null, kind: 'model', status: 'retry_wait', attempt_count: 2, max_attempts: 0, error_code: null, next_retry_at: null }] })).toThrow('任务响应格式不正确')
})
