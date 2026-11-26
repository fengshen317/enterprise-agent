import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import SchedulesView from './SchedulesView.vue'
import * as api from '../api/schedules'
import * as chat from '../api/chat'

vi.mock(import('../api/schedules'), async original => ({ ...await original(), listSchedules: vi.fn(), getSchedule: vi.fn(), createSchedule: vi.fn(), updateSchedule: vi.fn(), updateScheduleEnabled: vi.fn(), updateScheduleWeChatPush: vi.fn(), deleteSchedule: vi.fn(), runScheduleNow: vi.fn() }))
vi.mock('../api/chat', () => ({ listConversations: vi.fn(), createConversation: vi.fn() }))

const schedule: api.Schedule = {
  id: 's1',
  conversation_id: 'c1',
  conversation_title: '每日工作',
  name: '晨报',
  prompt: '总结今天的重点',
  cron_expression: '0 9 * * *',
  timezone: 'Asia/Shanghai',
  calendar_mode: 'all_days',
  calendar_waiting_year: null,
  revision: 1,
  enabled: true,
  push_to_wechat: false,
  push_channels: [],
  next_run_at: '2026-11-12T01:00:00Z',
  last_run_at: null,
  last_run_id: null,
  last_status: null,
  last_error_code: null,
  created_at: '2026-11-11T00:00:00Z',
  updated_at: '2026-11-11T00:00:00Z',
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listSchedules).mockResolvedValue([schedule])
  vi.mocked(chat.listConversations).mockResolvedValue([{ id: 'c1', title: '每日工作', status: 'active', updated_at: '2026-11-11T00:00:00Z' }])
  vi.mocked(chat.createConversation).mockResolvedValue({ id: 'c2', title: '新对话', status: 'active', updated_at: '2026-11-11T00:00:00Z' })
  vi.mocked(api.createSchedule).mockResolvedValue(schedule)
  vi.mocked(api.updateSchedule).mockResolvedValue({ ...schedule, enabled: false })
  vi.mocked(api.updateScheduleEnabled).mockResolvedValue({ ...schedule, enabled: false })
  vi.mocked(api.updateScheduleWeChatPush).mockResolvedValue({ ...schedule, push_to_wechat: true })
  vi.mocked(api.deleteSchedule).mockResolvedValue()
  vi.mocked(api.runScheduleNow).mockResolvedValue({ run: { id: 'r1', conversation_id: 'c1', status: 'pending', model: 'test', error_code: null, error_message: null }, message: { id: 'm1', conversation_id: 'c1', run_id: 'r1', sequence: 2, role: 'assistant', content: '', status: 'streaming' } })
})

async function setup(path = '/schedules') {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/schedules', component: SchedulesView }, { path: '/chat/:id', component: { template: '<div>问答</div>' } }] })
  await router.push(path)
  const wrapper = mount(SchedulesView, { global: { plugins: [createPinia(), router] } })
  await flushPromises()
  return wrapper
}

it('读取并展示当前用户的定时任务', async () => {
  const wrapper = await setup()
  expect(wrapper.text()).toContain('晨报')
  expect(wrapper.text()).toContain('每日工作')
  expect(wrapper.text()).toContain('0 9 * * *')
  expect(wrapper.text()).toContain('已启用')
  wrapper.unmount()
})

it('创建任务时提交固定对话、Cron 和时区', async () => {
  const wrapper = await setup()
  await wrapper.findAll('.schedules__primary')[0]!.trigger('click')
  await wrapper.get('[name="scheduleName"]').setValue('每日晨报')
  await wrapper.get('[name="schedulePrompt"]').setValue('总结今天的重点')
  await wrapper.get('[name="scheduleCalendar"]').setValue('cn_workday')
  await wrapper.get('form.schedules__form').trigger('submit')
  await flushPromises()
  expect(api.createSchedule).toHaveBeenCalledWith({
    name: '每日晨报',
    prompt: '总结今天的重点',
    conversation_id: 'c1',
    cron_expression: '0 9 * * *',
    timezone: 'Asia/Shanghai',
    calendar_mode: 'cn_workday',
    enabled: true,
    push_to_wechat: false,
    push_channels: [],
  })
  expect(wrapper.text()).toContain('定时任务已创建')
  wrapper.unmount()
})

it('可以新建对话并作为固定对话提交', async () => {
  const wrapper = await setup()
  await wrapper.findAll('.schedules__primary')[0]!.trigger('click')
  await wrapper.get('.schedules__new-conversation').trigger('click')
  await flushPromises()
  expect(chat.createConversation).toHaveBeenCalledTimes(1)
  expect((wrapper.get('[name="scheduleConversation"]').element as HTMLSelectElement).value).toBe('c2')
  expect(wrapper.text()).toContain('已新建对话并固定')
  await wrapper.get('[name="scheduleName"]').setValue('新任务')
  await wrapper.get('[name="schedulePrompt"]').setValue('新对话定时问题')
  await wrapper.get('form.schedules__form').trigger('submit')
  await flushPromises()
  expect(api.createSchedule).toHaveBeenCalledWith(expect.objectContaining({ conversation_id: 'c2' }))
  wrapper.unmount()
})

it('行内可以切换微信推送、停用、立即执行和删除', async () => {
  const wrapper = await setup()
  let actions = wrapper.findAll('.schedules__row-actions button')
  await actions[2]!.trigger('click')
  await flushPromises()
  expect(api.updateScheduleWeChatPush).toHaveBeenCalledWith('s1', true)
  expect(wrapper.text()).toContain('已开启微信推送')
  actions = wrapper.findAll('.schedules__row-actions button')
  await actions[3]!.trigger('click')
  await flushPromises()
  expect(api.updateScheduleEnabled).toHaveBeenCalledWith('s1', false)
  expect(api.updateSchedule).not.toHaveBeenCalled()
  actions = wrapper.findAll('.schedules__row-actions button')
  await actions[0]!.trigger('click')
  await flushPromises()
  expect(api.runScheduleNow).toHaveBeenCalledWith('s1')
  expect(wrapper.text()).toContain('已触发一次运行')
  actions = wrapper.findAll('.schedules__row-actions button')
  await actions[4]!.trigger('click')
  await flushPromises()
  expect(api.deleteSchedule).toHaveBeenCalledWith('s1')
  wrapper.unmount()
})

it('编辑会回填并提交完整字段', async () => {
  const wrapper = await setup()
  await wrapper.findAll('.schedules__row-actions button')[1]!.trigger('click')
  expect((wrapper.get('[name="scheduleName"]').element as HTMLInputElement).value).toBe('晨报')
  await wrapper.get('[name="schedulePrompt"]').setValue('新的问题')
  await wrapper.get('form.schedules__form').trigger('submit')
  await flushPromises()
  expect(api.updateSchedule).toHaveBeenCalledWith('s1', {
    name: '晨报',
    prompt: '新的问题',
    conversation_id: 'c1',
    cron_expression: '0 9 * * *',
    timezone: 'Asia/Shanghai',
    calendar_mode: 'all_days',
    revision: 1,
    enabled: true,
    push_to_wechat: false,
    push_channels: [],
  })
  wrapper.unmount()
})

it('长指令在列表只占两行，详情保留全文并标明等待交易日历', async () => {
  const prompt = '监控市场。\n'.repeat(150)
  vi.mocked(api.listSchedules).mockResolvedValue([
    { ...schedule, prompt, calendar_mode: 'cn_a_share', calendar_waiting_year: 2027, next_run_at: null },
  ])
  const wrapper = await setup()
  expect(wrapper.get('.schedules__excerpt').text().length).toBeLessThan(130)
  expect(wrapper.get('details').attributes('open')).toBeUndefined()
  expect(wrapper.get('.schedules__full-prompt').element.textContent?.trim()).toBe(prompt.trim())
  expect(wrapper.text()).toContain('等待 2027 年日历')
  wrapper.unmount()
})

it('按链接从服务端读取所选任务，支持首页以外的历史任务', async () => {
  vi.mocked(api.getSchedule).mockResolvedValue({ ...schedule, id: 's2', name: '历史任务' })
  const wrapper = await setup('/schedules?schedule=s2')
  expect(api.getSchedule).toHaveBeenCalledWith('s2')
  expect(api.listSchedules).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('历史任务')
  await wrapper.get('a[href="/schedules"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('晨报')
  expect(wrapper.text()).not.toContain('历史任务')
  wrapper.unmount()
})
