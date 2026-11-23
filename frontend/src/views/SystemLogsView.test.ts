import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { getSystemLog, listSystemLogs, type SystemLogDetail, type SystemLogEvent, type SystemLogPage } from '../api/systemLogs'
import { useAuthStore } from '../stores/auth'
import SystemLogsView from './SystemLogsView.vue'

vi.mock('../api/systemLogs', async importOriginal => ({
  ...await importOriginal<typeof import('../api/systemLogs')>(),
  listSystemLogs: vi.fn(),
  getSystemLog: vi.fn(),
}))

const failed: SystemLogEvent = {
  id: '41361f62-a7c2-49e3-bf1e-70ffc177cb28',
  occurred_at: '2026-11-19T03:24:17.306Z', level: 'error', component: 'connector',
  event: 'tool_finished', outcome: 'error', error_code: 'CONNECTOR_INVALID_OUTPUT', duration_ms: 704,
  request_id: 'request-1', run_id: 'run-1', tool_call_id: 'tool-1', connector_key: 'ima',
  tool_name: 'ima__search_knowledge_base', task_execution_id: null,
  details: { phase: 'response_validation', upstream_status: 200, upstream_code: 0, rule: 'knowledge_base_id' },
}
const accepted: SystemLogEvent = {
  ...failed, id: 'accepted', occurred_at: '2026-11-19T03:24:16.104Z', level: 'info', component: 'http',
  event: 'http_request_accepted', outcome: 'success', error_code: null, connector_key: null, tool_name: null,
  details: { method: 'POST', route: '/api/conversations/{id}/runs', status_code: 200 },
}
const counts = { all: 51, error: 1, warning: 0, success: 50 }
const detail: SystemLogDetail = { event: failed, related: [failed, accepted], related_truncated: true }
const clipboard = vi.fn().mockResolvedValue(undefined)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listSystemLogs).mockResolvedValue({ items: [failed], total: 51, counts, next_cursor: 'opaque' })
  vi.mocked(getSystemLog).mockResolvedValue(detail)
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false } },
  })
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: clipboard } })
})

afterEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard')
})

async function setup(role: 'admin' | 'member' = 'admin') {
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.session = {
    csrf_token: 'csrf', expires_at: '', user: { id: 'user', username: 'user', display_name: '测试用户' },
    workspaces: [
      { id: 'w1', slug: 'one', name: '空间一', status: 'active', role },
      { id: 'w2', slug: 'two', name: '空间二', status: 'active', role: 'admin' },
    ],
  }
  auth.selectedWorkspaceId = 'w1'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin/system-logs', component: SystemLogsView },
      { path: '/admin/members', component: { template: '<p>成员与权限</p>' } },
      { path: '/chat', component: { template: '<p>问答</p>' } },
    ],
  })
  await router.push('/admin/system-logs')
  const wrapper = mount(SystemLogsView, { attachTo: document.body, global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper, auth }
}

it('按编号、时间、来源、级别和结果查询，并在筛选后使用新游标', async () => {
  const { wrapper } = await setup()
  expect(listSystemLogs).toHaveBeenLastCalledWith(expect.objectContaining({ days: 1, limit: 50, cursor: undefined }))
  const pages = wrapper.get('nav[aria-label="事件分页"]')
  const [previous, next] = pages.findAll('button')
  await next!.trigger('click')
  expect(listSystemLogs).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: 'opaque' }))
  await flushPromises()
  expect(pages.text()).toContain('2 / 2')
  await previous!.trigger('click')
  expect(listSystemLogs).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: undefined }))
  await flushPromises()

  await wrapper.get('[aria-label="时间范围"]').setValue('7')
  await wrapper.get('[aria-label="事件来源"]').setValue('tool')
  await wrapper.get('[aria-label="日志级别"]').setValue('error')
  await wrapper.get('.system-logs__summary-cell[aria-pressed="false"]').trigger('click')
  const search = wrapper.get('input[type="search"]')
  await search.setValue('  tool-1  ')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(listSystemLogs).toHaveBeenLastCalledWith({
    days: 7, limit: 50, cursor: undefined, component: 'tool', level: 'error', outcome: 'error', q: 'tool-1',
  })
  expect(wrapper.get('[aria-label="执行结果"]').element).toHaveProperty('value', 'error')
  expect(wrapper.get('.system-logs__summary-cell').text()).toContain('51')
  wrapper.unmount()
})

it('展示脱敏详情及按时间排序的关联事件，可复制和按 Run 查询', async () => {
  const { wrapper } = await setup()
  await wrapper.get('.system-logs__event').trigger('click')
  await flushPromises()
  expect(getSystemLog).toHaveBeenCalledWith(failed.id)
  const drawer = wrapper.get('dialog[open]')
  expect(drawer.get('[aria-label="诊断结果"]').text()).toContain('上游已响应，适配器校验失败')
  expect(drawer.get('[aria-label="诊断结果"]').text()).toContain('业务码0')
  expect(drawer.get('[aria-label="诊断结果"]').text()).toContain('知识库 ID 字段')
  expect(drawer.findAll('.system-logs__timeline li').map(row => row.get('strong').text())).toEqual([
    '问答请求已受理', '连接器响应校验失败',
  ])
  expect(drawer.text()).toContain('关联事件较多')
  await drawer.get('.system-logs__drawer-footer button').trigger('click')
  await flushPromises()
  expect(clipboard).toHaveBeenCalledWith(failed.id)
  expect(drawer.text()).toContain('诊断编号已复制')
  await drawer.get('.system-logs__button--primary').trigger('click')
  expect(wrapper.find('dialog[open]').exists()).toBe(false)
  expect(listSystemLogs).toHaveBeenLastCalledWith(expect.objectContaining({ days: 30, q: 'run-1' }))
  expect(document.activeElement).toBe(wrapper.get('input[type="search"]').element)
  wrapper.unmount()
})

it('空间切换时清除旧筛选和详情，忽略上一空间的延迟响应', async () => {
  const { auth, wrapper } = await setup()
  let settleDetail: (result: SystemLogDetail) => void = () => {}
  let settleList: (result: SystemLogPage) => void = () => {}
  vi.mocked(getSystemLog).mockReturnValueOnce(new Promise(resolve => { settleDetail = resolve }))
  vi.mocked(listSystemLogs)
    .mockReturnValueOnce(new Promise(resolve => { settleList = resolve }))
    .mockResolvedValueOnce({ items: [], total: 0, counts: { all: 0, success: 0, warning: 0, error: 0 }, next_cursor: null })

  await wrapper.get('.system-logs__event').trigger('click')
  await wrapper.get('input[type="search"]').setValue('previous-space')
  await wrapper.get('form').trigger('submit')
  expect(wrapper.find('dialog[open]').exists()).toBe(true)
  auth.selectedWorkspaceId = 'w2'
  await flushPromises()
  expect(listSystemLogs).toHaveBeenLastCalledWith(expect.objectContaining({ days: 1, q: undefined, cursor: undefined }))
  expect(wrapper.get<HTMLInputElement>('input[type="search"]').element.value).toBe('')
  expect(wrapper.find('dialog[open]').exists()).toBe(false)
  settleDetail(detail)
  settleList({ items: [failed], total: 51, counts, next_cursor: 'old' })
  await flushPromises()
  expect(wrapper.text()).toContain('没有符合条件的事件')
  expect(wrapper.text()).not.toContain(failed.id)
  wrapper.unmount()
})

it('普通成员无法读取诊断事件', async () => {
  const { wrapper } = await setup('member')
  expect(listSystemLogs).not.toHaveBeenCalled()
  expect(wrapper.get('[role="alert"]').text()).toContain('仅当前工作空间的管理员')
  wrapper.unmount()
})
