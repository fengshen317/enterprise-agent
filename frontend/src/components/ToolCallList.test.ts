import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import ToolCallList from './ToolCallList.vue'
import { getToolCalls, type ToolCall } from '../api/tools'

vi.mock('../api/tools', () => ({ getToolCalls: vi.fn() }))
beforeEach(() => { vi.resetAllMocks() })
const row: ToolCall = { id: 'a1', run_id: 'r1', tool_name: 'get_my_run_stats', days: 7, status: 'completed', result: JSON.stringify({ days: 7, total: 2, counts: { completed: 1, running: 1 } }), error_code: null, created_at: '', finished_at: '' }

it('展开后才查询并展示真实执行结果', async () => {
  vi.mocked(getToolCalls).mockResolvedValue([row])
  const wrapper = mount(ToolCallList, { props: { runId: 'r1', messageStatus: 'completed' } })
  expect(getToolCalls).not.toHaveBeenCalled()
  wrapper.get('details').element.open = true
  await wrapper.get('details').trigger('toggle')
  await flushPromises()
  expect(getToolCalls).toHaveBeenCalledWith('r1')
  expect(wrapper.text()).toContain('查询成功')
  expect(wrapper.text()).toContain('共 2 次运行')
  expect(wrapper.text()).toContain('包含本次')
  wrapper.unmount()
})
it('失败和没有调用不会展示成查询成功', async () => {
  vi.mocked(getToolCalls).mockResolvedValue([{ ...row, status: 'failed', result: null, error_code: 'TOOL_TIMEOUT' }])
  const wrapper = mount(ToolCallList, { props: { runId: 'r1', messageStatus: 'streaming' } })
  wrapper.get('details').element.open = true
  await wrapper.get('details').trigger('toggle')
  await flushPromises()
  expect(wrapper.text()).toContain('查询超时')
  expect(wrapper.text()).not.toContain('查询成功')
  vi.mocked(getToolCalls).mockResolvedValue([])
  await wrapper.setProps({ messageStatus: 'interrupted' })
  await flushPromises()
  expect(wrapper.text()).toContain('尚无工具调用')
  wrapper.unmount()
})
it('查询异常显示可重试提示', async () => {
  vi.mocked(getToolCalls).mockRejectedValue(new Error('无权读取'))
  const wrapper = mount(ToolCallList, { props: { runId: 'r1', messageStatus: 'streaming' } })
  wrapper.get('details').element.open = true
  await wrapper.get('details').trigger('toggle')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('无权读取')
  vi.mocked(getToolCalls).mockResolvedValue([row])
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('查询成功')
  wrapper.unmount()
})

it('展示知识库检索来源摘要', async () => {
  vi.mocked(getToolCalls).mockResolvedValue([
    { ...row, id: 'kb', tool_name: 'search_knowledge_base', days: null, result: JSON.stringify({ query: '年假', chunks: [{ document_name: '员工手册.pdf' }, { document_name: '制度.pdf' }] }) },
  ])
  const wrapper = mount(ToolCallList, { props: { runId: 'r1', messageStatus: 'completed' } })
  wrapper.get('details').element.open = true
  await wrapper.get('details').trigger('toggle')
  await flushPromises()
  expect(wrapper.text()).toContain('知识库检索')
  expect(wrapper.text()).toContain('命中 2 条来源')
  expect(wrapper.text()).toContain('员工手册.pdf')
  wrapper.unmount()
})

it('问答生成时自动展示计划和真实子 Agent 状态，刷新后仍可读取', async () => {
  vi.mocked(getToolCalls).mockResolvedValue([
    { ...row, id: 'plan', tool_name: 'update_plan', result: JSON.stringify({ steps: [{ title: '分析并复核', status: 'in_progress' }] }) },
    { ...row, id: 'child', tool_name: 'delegate_agent', status: 'running', result: JSON.stringify({ title: '检查实现风险', role: 'reviewer' }) },
  ])
  const wrapper = mount(ToolCallList, { props: { runId: 'r1', messageStatus: 'streaming' } })
  await flushPromises()
  expect(wrapper.get('[aria-label="工作计划"]').text()).toContain('分析并复核 · 进行中')
  expect(wrapper.get('[aria-label="子 Agent 协作"]').text()).toContain('检查实现风险 · 复核员')
  expect(wrapper.text()).toContain('执行中')
  vi.mocked(getToolCalls).mockResolvedValue([
    { ...row, id: 'child', tool_name: 'delegate_agent', result: JSON.stringify({ title: '检查实现风险', role: 'reviewer', output: '需要关注取消传播' }) },
  ])
  await wrapper.setProps({ messageStatus: 'completed' })
  await flushPromises()
  expect(wrapper.text()).toContain('需要关注取消传播')
  expect(wrapper.text()).toContain('已完成')
  wrapper.unmount()
})
it('展示网络重试等待和失败子任务，不能将失败显示为已完成', async () => {
  vi.mocked(getToolCalls).mockResolvedValue([
    { ...row, id: 'retry', tool_name: 'model_request', status: 'running', result: JSON.stringify({ attempt: 1, max_attempts: 3, next_retry_at: new Date(Date.now() + 60000).toISOString() }) },
    { ...row, id: 'child', tool_name: 'delegate_agent', status: 'failed', error_code: 'AGENT_FAILED', result: JSON.stringify({ title: '分析', role: 'analyst' }) },
  ])
  const wrapper = mount(ToolCallList, { props: { runId: 'r1', messageStatus: 'streaming' } })
  await flushPromises()
  expect(wrapper.text()).toContain('秒后重试')
  expect(wrapper.text()).toContain('已请求 1/3 次')
  expect(wrapper.get('[aria-label="子 Agent 协作"]').text()).toContain('失败')
  expect(wrapper.get('[aria-label="子 Agent 协作"]').text()).not.toContain('已完成')
  wrapper.unmount()
})
