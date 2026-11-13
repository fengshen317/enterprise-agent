import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TasksView from './TasksView.vue'
import * as api from '../api/tasks'
import type { TaskSnapshot } from '../api/tasks'

vi.mock('../api/tasks', async () => {
  const actual = await vi.importActual<typeof import('../api/tasks')>('../api/tasks')
  return { ...actual, listTasks: vi.fn(), createTask: vi.fn(), getTask: vi.fn(), confirmTask: vi.fn(), controlTask: vi.fn(), updateTaskPlan: vi.fn(), watchTask: vi.fn() }
})
const draft: TaskSnapshot = {
  task: { id: 't1', goal: '分析最近7天的运行情况', workspace_id: 'w1', owner_user_id: 'u1', created_at: '' },
  run: { id: 'r1', status: 'awaiting_confirmation', error_code: null, result: null, deadline_at: null, next_retry_at: null, fencing_token: 1, lease_owner: null, version: 1 },
  plan: { revision: 1, digest: 'digest-1', steps: [{ key: 's1', title: '查询统计', kind: 'tool', depends_on: [], instruction: '查询本人当前空间最近7天的统计。', tool_name: 'get_my_run_stats', arguments: { days: 7 } }] },
  steps: [{ key: 's1', title: '查询统计', kind: 'tool', depends_on: [], status: 'pending', attempt_count: 0, result: null, error_code: null, next_retry_at: null }],
  actions: [],
  last_event_sequence: 1,
}
const running: TaskSnapshot = { ...draft, run: { ...draft.run, status: 'running', version: 2 } }
const cancelled: TaskSnapshot = { ...draft, run: { ...draft.run, status: 'cancelled', version: 3 } }
let dispose: (() => void) | undefined
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listTasks).mockResolvedValue([draft])
  vi.mocked(api.getTask).mockResolvedValue(draft)
  vi.mocked(api.createTask).mockResolvedValue(draft)
  vi.mocked(api.confirmTask).mockResolvedValue(running)
  vi.mocked(api.controlTask).mockResolvedValue(cancelled)
  vi.mocked(api.watchTask).mockReturnValue(vi.fn())
})
afterEach(() => { dispose?.(); dispose = undefined; vi.useRealTimers() })
async function setup(path = '/tasks/t1') {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/tasks/:taskId?', component: TasksView }] })
  await router.push(path)
  const wrapper = mount(TasksView, { global: { plugins: [router] } })
  dispose = () => wrapper.unmount()
  await flushPromises()
  return { router, wrapper }
}
it('明确提交目标才创建任务，生成计划后不自动执行', async () => {
  const { wrapper, router } = await setup('/tasks')
  expect(api.createTask).not.toHaveBeenCalled()
  await wrapper.get('#task-goal').setValue('分析最近7天的运行情况')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(api.createTask).toHaveBeenCalledWith('分析最近7天的运行情况', expect.any(String))
  expect(router.currentRoute.value.path).toBe('/tasks/t1')
  expect(wrapper.text()).toContain('确认计划并执行')
  expect(api.confirmTask).not.toHaveBeenCalled()
})
it('用户确认精确计划版本与摘要后才推进', async () => {
  const { wrapper } = await setup()
  expect(wrapper.text()).toContain('确认前不会执行工具')
  await wrapper.findAll('.tasks__confirm button').find(button => button.text() === '确认计划并执行')!.trigger('click')
  await flushPromises()
  expect(api.confirmTask).toHaveBeenCalledWith('t1', 1, 'digest-1', 'r1')
  expect(wrapper.get('.tasks__status').text()).toBe('正在执行')
  expect(wrapper.find('.tasks__confirm').exists()).toBe(false)
})
it('网络失败保留目标并复用提交幂等键', async () => {
  vi.mocked(api.createTask).mockRejectedValue(new Error('网络暂时不可用'))
  const { wrapper } = await setup('/tasks')
  await wrapper.get('#task-goal').setValue('保留目标')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.get<HTMLTextAreaElement>('#task-goal').element.value).toBe('保留目标')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(api.createTask).toHaveBeenCalledTimes(2)
  expect(vi.mocked(api.createTask).mock.calls[0]?.[1]).toBe(vi.mocked(api.createTask).mock.calls[1]?.[1])
})
it('展示真实等待倒计时，轮询不会重新提交任务', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-04T00:00:00Z'))
  const waiting: TaskSnapshot = { ...draft, run: { ...draft.run, status: 'retry_wait', next_retry_at: '2026-11-04T00:00:05Z' } }
  vi.mocked(api.getTask).mockResolvedValue(waiting)
  const { wrapper } = await setup()
  expect(wrapper.get('.tasks__waiting').text()).toContain('5 秒后重试')
  await vi.advanceTimersByTimeAsync(2000)
  await flushPromises()
  expect(wrapper.get('.tasks__waiting').text()).toContain('3 秒后重试')
  expect(api.createTask).not.toHaveBeenCalled()
  expect(api.confirmTask).not.toHaveBeenCalled()
})
it('进度读取异常只显示连接提示，不把任务改成失败', async () => {
  vi.useFakeTimers()
  vi.mocked(api.getTask).mockResolvedValueOnce(running).mockRejectedValue(new Error('连接超时'))
  const { wrapper } = await setup()
  await vi.advanceTimersByTimeAsync(1000)
  await flushPromises()
  expect(wrapper.get('.tasks__status').text()).toBe('正在执行')
  expect(wrapper.get('.tasks__connection').text()).toContain('正在重新连接')
  expect(api.createTask).not.toHaveBeenCalled()
})
it('停止任务后迟到的旧快照不会恢复执行中状态', async () => {
  vi.useFakeTimers()
  let release: ((value: TaskSnapshot) => void) | undefined
  vi.mocked(api.getTask).mockResolvedValueOnce(running).mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
  const { wrapper } = await setup()
  await vi.advanceTimersByTimeAsync(1000)
  await wrapper.get('.tasks__stop').trigger('click')
  await flushPromises()
  expect(api.controlTask).toHaveBeenCalledWith('t1', 'cancel', 'r1', 2)
  expect(wrapper.get('.tasks__status').text()).toBe('已停止')
  release?.(running)
  await flushPromises()
  expect(wrapper.get('.tasks__status').text()).toBe('已停止')
})
it('切换任务后迟到的结果不覆盖新任务', async () => {
  let release: ((value: TaskSnapshot) => void) | undefined
  const other: TaskSnapshot = { ...draft, task: { ...draft.task, id: 't2', goal: '另一项任务' } }
  vi.mocked(api.getTask).mockImplementation(id => id === 't1' ? new Promise(resolve => { release = resolve }) : Promise.resolve(other))
  const { wrapper, router } = await setup()
  await router.push('/tasks/t2')
  await flushPromises()
  release?.(draft)
  await flushPromises()
  expect(wrapper.get('.tasks__top h1').text()).toBe('另一项任务')
})
it('失败后手动新建执行尝试，保留已有步骤展示', async () => {
  const failed: TaskSnapshot = { ...draft, run: { ...draft.run, status: 'failed', error_code: 'RETRY_EXHAUSTED' }, steps: [{ ...draft.steps[0]!, status: 'succeeded', result: '真实聚合结果', attempt_count: 1 }] }
  const retried: TaskSnapshot = { ...draft, run: { ...draft.run, id: 'r2', status: 'pending' } }
  vi.mocked(api.getTask).mockResolvedValue(failed)
  vi.mocked(api.controlTask).mockResolvedValue(retried)
  const { wrapper } = await setup()
  expect(wrapper.text()).toContain('真实聚合结果')
  expect(wrapper.text()).toContain('已达到重试次数上限')
  const button = wrapper.findAll('.tasks__controls button').find(item => item.text() === '新建执行尝试')!
  await button.trigger('click')
  await flushPromises()
  expect(api.controlTask).toHaveBeenCalledWith('t1', 'retry', 'r1', 1)
  expect(wrapper.get('.tasks__status').text()).toBe('等待规划')
})
it('卸载后停止轮询，不会产生额外模型或任务请求', async () => {
  vi.useFakeTimers()
  await setup()
  dispose?.(); dispose = undefined
  const count = vi.mocked(api.getTask).mock.calls.length
  await vi.advanceTimersByTimeAsync(5000)
  expect(api.getTask).toHaveBeenCalledTimes(count)
  expect(api.createTask).not.toHaveBeenCalled()
})
it('任务数据格式错误不作为合法进度展示', () => {
  expect(() => api.parseTask({ ...draft, run: { ...draft.run, status: 'unknown' } })).toThrow('任务响应格式不正确')
  expect(() => api.parseTask({ ...draft, plan: { ...draft.plan, steps: [{ ...draft.plan?.steps[0], depends_on: 's1' }] } })).toThrow('任务响应格式不正确')
})

it('修改计划必须保存新版本，不能自动确认或执行', async () => {
  const updated: TaskSnapshot = { ...draft, plan: { ...draft.plan!, revision: 2, digest: 'digest-2' }, run: { ...draft.run, version: 2 }, last_event_sequence: 2 }
  vi.mocked(api.updateTaskPlan).mockResolvedValue(updated)
  const { wrapper } = await setup()
  await wrapper.get('.tasks__edit').trigger('click')
  const definition = { steps: [{ ...draft.plan!.steps[0]!, title: '调整后的统计' }] }
  await wrapper.get('#plan-json').setValue(JSON.stringify(definition))
  await wrapper.get('.tasks__editor').trigger('submit')
  await flushPromises()
  expect(api.updateTaskPlan).toHaveBeenCalledWith('t1', definition, 'r1', 1)
  expect(wrapper.text()).toContain('版本 2')
  expect(api.confirmTask).not.toHaveBeenCalled()
})
it('计划编辑格式错误保留草案且不发请求', async () => {
  const { wrapper } = await setup()
  await wrapper.get('.tasks__edit').trigger('click')
  await wrapper.get('#plan-json').setValue('{ invalid')
  await wrapper.get('.tasks__editor').trigger('submit')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('JSON 格式不正确')
  expect(api.updateTaskPlan).not.toHaveBeenCalled()
})
it('SSE 正常传输时停止轮询，旧订阅在取消后失效', async () => {
  vi.useFakeTimers()
  const { wrapper } = await setup()
  const receive = vi.mocked(api.watchTask).mock.calls[0]![2]
  receive(running)
  await flushPromises()
  const count = vi.mocked(api.getTask).mock.calls.length
  await vi.advanceTimersByTimeAsync(2000)
  expect(api.getTask).toHaveBeenCalledTimes(count)
  await wrapper.get('.tasks__stop').trigger('click')
  await flushPromises()
  receive({ ...running, run: { ...running.run, version: 99 } })
  await flushPromises()
  expect(wrapper.get('.tasks__status').text()).toBe('已停止')
})
it('新执行后旧执行的事件游标不能覆盖当前快照', async () => {
  const { wrapper } = await setup()
  const receive = vi.mocked(api.watchTask).mock.calls[0]![2]
  receive({ ...running, run: { ...running.run, id: 'r2' }, last_event_sequence: 10 })
  receive({ ...draft, run: { ...draft.run, version: 99 }, last_event_sequence: 2 })
  await flushPromises()
  expect(wrapper.get('.tasks__status').text()).toBe('正在执行')
})

it('网络请求次数使用动作摘要，不把步骤启动次数当成请求次数', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-04T00:00:00Z'))
  const waiting: TaskSnapshot = { ...draft, run: { ...draft.run, status: 'retry_wait', next_retry_at: '2026-11-04T00:00:05Z' }, actions: [{ id: 'a1', step_key: 's1', kind: 'tool', status: 'retry_wait', attempt_count: 2, max_attempts: 4, error_code: 'NETWORK_TIMEOUT', next_retry_at: '2026-11-04T00:00:05Z' }], steps: [{ ...draft.steps[0]!, status: 'retry_wait', attempt_count: 1 }] }
  vi.mocked(api.getTask).mockResolvedValue(waiting)
  const { wrapper } = await setup()
  expect(wrapper.get('.tasks__waiting').text()).toContain('已尝试 2/4 次')
  expect(wrapper.get('.tasks__waiting').text()).toContain('请求连接或读取超时')
  expect(wrapper.get('.tasks__actions').text()).toContain('工具查询')
  expect(wrapper.get('.tasks__actions').text()).toContain('已尝试 2/4 次')
  expect(wrapper.get('.tasks__step-progress').text()).toContain('步骤启动 1 次')
  expect(wrapper.get('.tasks__waiting').text()).not.toContain('已尝试 1/3 次')
})
it('暂停或失败不会把保留的重试时间显示为正在自动重试', async () => {
  const paused: TaskSnapshot = { ...draft, run: { ...draft.run, status: 'paused', error_code: 'RATE_LIMITED' }, actions: [{ id: 'a1', step_key: null, kind: 'plan', status: 'retry_wait', attempt_count: 1, max_attempts: 3, error_code: 'RATE_LIMITED', next_retry_at: '2026-11-05T00:00:00Z' }] }
  vi.mocked(api.getTask).mockResolvedValue(paused)
  const { wrapper } = await setup()
  expect(wrapper.get('.tasks__waiting').text()).toContain('恢复后仍需遵守')
  const receive = vi.mocked(api.watchTask).mock.calls[0]![2]
  receive({ ...paused, run: { ...paused.run, status: 'failed', version: 2 }, last_event_sequence: 2 })
  await flushPromises()
  expect(wrapper.find('.tasks__waiting').exists()).toBe(false)
  expect(wrapper.get('.tasks__actions').text()).toContain('随任务中断')
})
