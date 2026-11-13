import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'
import ChatView from './ChatView.vue'
import * as api from '../api/chat'
import * as agentApi from '../api/agents'
import type { Message, Snapshot } from '../api/chat'

vi.mock('../api/agents', () => ({ listAgents: vi.fn() }))
vi.mock('../api/chat', async () => {
  const original = await vi.importActual<typeof import('../api/chat')>('../api/chat')
  return { ...original, listConversations: vi.fn(), getMessages: vi.fn(), getRun: vi.fn(), createRun: vi.fn(), cancelRun: vi.fn(), watchRun: vi.fn(), createConversation: vi.fn(), listModels: vi.fn(), setConversationModel: vi.fn(), regenerateMessage: vi.fn(), editMessage: vi.fn() }
})
const first: Message = { id: 'm1', conversation_id: 'c1', run_id: null, sequence: 1, role: 'user', content: '旧问题', status: 'completed' }
const old: Snapshot = { run: { id: 'r1', conversation_id: 'c1', status: 'completed', model: 'test', error_code: null, error_message: null }, message: { ...first, id: 'm2', run_id: 'r1', sequence: 2, role: 'assistant', content: '旧回答' } }
const current: Snapshot = { run: { ...old.run, id: 'r2', status: 'running' }, message: { ...old.message, id: 'm4', run_id: 'r2', sequence: 4, content: '部分内容', status: 'streaming' } }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(agentApi.listAgents).mockResolvedValue([])
  vi.mocked(api.listModels).mockResolvedValue([{ id: 'deepseek-flash', max_tokens: 4096, thinking: false, is_default: true }, { id: 'deepseek-reasoner', max_tokens: 16384, thinking: true, is_default: false }])
  vi.mocked(api.listConversations).mockResolvedValue([{ id: 'c1', title: '测试对话', status: 'active', updated_at: '' }])
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, old.message], has_more: false, active_run_id: null })
  vi.mocked(api.getRun).mockImplementation(async id => id === 'r1' ? old : current)
  vi.mocked(api.watchRun).mockReturnValue(vi.fn())
})

it('恢复数据库历史，提交追问并停止生成', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('旧问题')
  expect(wrapper.text()).toContain('旧回答')
  vi.mocked(api.createRun).mockResolvedValue(current)
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, old.message, { ...first, id: 'm3', sequence: 3, content: '继续说明' }, current.message], has_more: false, active_run_id: 'r2' })
  await wrapper.get('textarea').setValue('继续说明')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(api.createRun).toHaveBeenCalledWith('c1', '继续说明', expect.any(String))
  expect(wrapper.text()).toContain('部分内容')
  const stopped: Snapshot = { run: { ...current.run, status: 'cancelled' }, message: { ...current.message, status: 'interrupted' } }
  vi.mocked(api.cancelRun).mockResolvedValue(stopped)
  await wrapper.get('.chat__stop').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('已停止')
  expect(wrapper.text()).toContain('部分回答 · 未完成')
  expect(api.cancelRun).toHaveBeenCalledWith('r2')
  wrapper.unmount()
})

it('助手回答中的 Markdown 表格按表格渲染', async () => {
  const table: Message = {
    ...old.message,
    id: 'm5',
    content: '**知识点日历**\n\n| 天 | 知识点 |\n|---|------|\n| 1 | 什么是 Deep Agent |',
  }
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, table], has_more: false, active_run_id: null })
  vi.mocked(api.getRun).mockResolvedValue({ run: old.run, message: table })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  const body = wrapper.findAll('.chat__body')[1]!
  expect(body.find('table').exists()).toBe(true)
  expect(body.findAll('th')).toHaveLength(2)
  expect(body.text()).toContain('什么是 Deep Agent')
  expect(body.text()).not.toContain('|---|')
  wrapper.unmount()
})

it('提交失败后保留输入并复用幂等键重试', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  vi.mocked(api.createRun).mockRejectedValue(new Error('网络暂时不可用'))
  await wrapper.get('textarea').setValue('保留的问题')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('保留的问题')
  expect(wrapper.text()).toContain('网络暂时不可用')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(vi.mocked(api.createRun).mock.calls[0]?.[2]).toBe(vi.mocked(api.createRun).mock.calls[1]?.[2])
  wrapper.unmount()
})


it('新对话在首次发送时创建，并导航到可刷新的地址', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat')
  vi.mocked(api.createConversation).mockResolvedValue({ id: 'c1', title: '新对话', status: 'active', updated_at: '' })
  vi.mocked(api.getMessages).mockResolvedValue({ items: [], has_more: false, active_run_id: null })
  vi.mocked(api.createRun).mockImplementation(async () => {
    vi.mocked(api.getMessages).mockResolvedValue({ items: [{ ...first, content: '新问题' }, current.message], has_more: false, active_run_id: 'r2' })
    return current
  })
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('从一个问题')
  await wrapper.get('textarea').setValue('新问题')
  await wrapper.get('form').trigger('submit')
  await vi.waitFor(() => expect(api.createRun).toHaveBeenCalled())
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/chat/c1')
  expect(api.createConversation).toHaveBeenCalledTimes(1)
  expect(wrapper.text()).toContain('新问题')
  wrapper.unmount()
})

it('切换对话后迟到的历史请求不会覆盖新对话', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  let release: ((value: api.MessagePage) => void) | undefined
  vi.mocked(api.getMessages).mockImplementation(async id => id === 'c1' ? new Promise(resolve => { release = resolve }) : { items: [{ ...first, id: 'other', conversation_id: 'c2', content: '另一段对话' }], has_more: false, active_run_id: null })
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  await router.push('/chat/c2')
  await flushPromises()
  release?.({ items: [first], has_more: false, active_run_id: null })
  await flushPromises()
  expect(wrapper.text()).toContain('另一段对话')
  expect(wrapper.text()).not.toContain('旧问题')
  wrapper.unmount()
})

it('失败后在同一对话重新生成，不追加一条用户消息', async () => {
  const failed: Snapshot = { run: { ...old.run, status: 'failed', error_message: '网络不可用' }, message: { ...old.message, status: 'interrupted' } }
  vi.mocked(api.getRun).mockResolvedValue(failed)
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, failed.message], has_more: false, active_run_id: null, context_usage: { estimated_characters: 1200, limit_characters: 100000, summary_characters: 0, injected_characters: 80, estimated_tokens: 300, estimate: true } })
  vi.mocked(api.regenerateMessage).mockResolvedValue(current)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('上下文估算')
  expect(wrapper.text()).toContain('上次实际输入约 300 tokens')
  expect((wrapper.get('select[aria-label="模型"]').element as HTMLSelectElement).disabled).toBe(false)
  const retry = wrapper.findAll('button').find(button => button.text() === '重试本次请求')
  expect(retry).toBeDefined()
  await retry!.trigger('click')
  await flushPromises()
  expect(api.regenerateMessage).toHaveBeenCalledWith('m2', expect.any(String))
  expect(api.createRun).not.toHaveBeenCalled()
  expect(router.currentRoute.value.path).toBe('/chat/c1')
  wrapper.unmount()
})
it('补充要求先停止正在运行的对话，再提交新要求', async () => {
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, current.message], has_more: false, active_run_id: 'r2' })
  vi.mocked(api.cancelRun).mockResolvedValue({ run: { ...current.run, status: 'cancelled' }, message: { ...current.message, status: 'interrupted' } })
  vi.mocked(api.createRun).mockResolvedValue(current)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('textarea').setValue('增加一轮复核')
  const button = wrapper.findAll('button').find(item => item.text() === '停止并补充要求')
  await button!.trigger('click')
  await flushPromises()
  expect(api.cancelRun).toHaveBeenCalledWith('r2')
  expect(api.createRun).toHaveBeenCalledWith('c1', '增加一轮复核', expect.any(String))
  expect(vi.mocked(api.cancelRun).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.createRun).mock.invocationCallOrder[0]!)
  wrapper.unmount()
})
