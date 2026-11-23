import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ChatView from './ChatView.vue'
import * as api from '../api/chat'
import * as agentApi from '../api/agents'
import type { Message, Snapshot } from '../api/chat'
import type { Approval } from '../api/approvals'
import { getMaterial } from '../api/materials'
import { getToolCalls, type ToolCall } from '../api/tools'
import { useAuthStore } from '../stores/auth'

vi.mock('../api/agents', () => ({ listAgents: vi.fn() }))
vi.mock('../api/tools', () => ({ getToolCalls: vi.fn() }))
vi.mock('../api/materials', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/materials')>()
  return { ...original, getMaterial: vi.fn() }
})
vi.mock('../api/chat', async () => {
  const original = await vi.importActual<typeof import('../api/chat')>('../api/chat')
  return { ...original, listConversations: vi.fn(), getMessages: vi.fn(), getRun: vi.fn(), createRun: vi.fn(), createRunWithFiles: vi.fn(), cancelRun: vi.fn(), watchRun: vi.fn(), createConversation: vi.fn() }
})
const first: Message = { id: 'm1', conversation_id: 'c1', run_id: null, sequence: 1, role: 'user', content: '旧问题', status: 'completed' }
const old: Snapshot = { run: { id: 'r1', conversation_id: 'c1', status: 'completed', model: 'test', error_code: null, error_message: null }, message: { ...first, id: 'm2', run_id: 'r1', sequence: 2, role: 'assistant', content: '旧回答' } }
const current: Snapshot = { run: { ...old.run, id: 'r2', status: 'running' }, message: { ...old.message, id: 'm4', run_id: 'r2', sequence: 4, content: '部分内容', status: 'streaming' } }
const imageAttachment = { id: 'f1', filename: '图表.png', media_type: 'image/png', size_bytes: 4, text_available: false, text_truncated: false }
const executionCalls = (runId: string, status: ToolCall['status'], title: string): ToolCall[] => [
  { id: runId + '-plan', run_id: runId, tool_name: 'update_plan', status: 'completed',
    days: null, error_code: null, created_at: '', finished_at: '',
    result: JSON.stringify({ steps: [{ title, status: status === 'completed' ? 'completed' : 'in_progress' }] }) },
  { id: runId + '-child', run_id: runId, tool_name: 'delegate_agent', status,
    days: null, error_code: null, created_at: '', finished_at: null,
    result: JSON.stringify({ title: '复核数据', role: 'reviewer' }) },
]

afterEach(() => { vi.useRealTimers() })

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.session = { user: { id: 'u1', username: 'user', display_name: '测试用户' }, workspaces: [], csrf_token: '', expires_at: '' }
  auth.selectedWorkspaceId = 'w1'
  vi.resetAllMocks()
  vi.mocked(getToolCalls).mockResolvedValue([])
  vi.mocked(agentApi.listAgents).mockResolvedValue([])
  vi.mocked(api.listConversations).mockResolvedValue([{ id: 'c1', title: '测试对话', status: 'active', updated_at: '' }])
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, old.message], has_more: false, active_run_id: null })
  vi.mocked(api.getRun).mockImplementation(async id => id === 'r1' ? old : current)
  vi.mocked(api.watchRun).mockReturnValue(vi.fn())
})

it('附件可移除，纯附件发送使用 multipart，网络失败时保留同一提交键', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  const file = new File(['test'], '图表.png', { type: 'image/png' })
  const text = new File(['demo'], '说明.txt', { type: 'text/plain' })
  const control = wrapper.get('input[type="file"]')
  Object.defineProperty(control.element, 'files', { configurable: true, value: [file, text] })
  await control.trigger('change')
  await wrapper.get('[aria-label="移除 说明.txt"]').trigger('click')
  vi.mocked(api.createRunWithFiles).mockRejectedValueOnce(new Error('连接超时')).mockResolvedValueOnce(current)
  await wrapper.get('.chat-composer').trigger('submit')
  await flushPromises()
  expect(wrapper.get('[aria-label="待发送附件"]').text()).toContain('图表.png')
  expect(wrapper.text()).toContain('连接超时')
  await wrapper.get('.chat-composer').trigger('submit')
  await flushPromises()
  const calls = vi.mocked(api.createRunWithFiles).mock.calls
  expect(calls[0]).toEqual(['c1', '请帮我查看这些附件。', expect.any(String), [file], []])
  expect(calls[1]).toEqual(calls[0])
  expect(wrapper.find('[aria-label="待发送附件"]').exists()).toBe(false)
  wrapper.unmount()
})

it('历史附件可下载并重新附加到当前对话', async () => {
  vi.mocked(api.getMessages).mockResolvedValue({ items: [{ ...first, attachments: [imageAttachment] }, old.message], has_more: false, active_run_id: null })
  vi.mocked(api.createRun).mockResolvedValue(current)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.get('[aria-label="下载 图表.png"]').attributes('href')).toContain('/attachments/f1/file?')
  await wrapper.get('[aria-label="再次附加 图表.png"]').trigger('click')
  await wrapper.get('.chat-composer').trigger('submit')
  await flushPromises()
  expect(api.createRun).toHaveBeenCalledWith('c1', '请帮我查看这些附件。', expect.any(String), ['f1'])
  wrapper.unmount()
})

it('历史回答直接显示生成文件并提供鉴权下载', async () => {
  const file = { ...imageAttachment, filename: '复盘.md', media_type: 'text/markdown', text_available: true }
  const message = { ...old.message, attachments: [file], content: '报告已保存' }
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, message], has_more: false, active_run_id: null })
  vi.mocked(api.getRun).mockResolvedValue({ ...old, message })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/chat/:conversationId?', component: ChatView },
    { path: '/materials/:id', component: { template: '<div />' } },
  ] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  const card = wrapper.get('[aria-label="生成的文件"]')
  expect(card.get('[aria-label="下载 复盘.md"]').attributes('href')).toContain('/attachments/f1/file?')
  expect(card.get('a[href="/materials/f1"]').text()).toBe('查看材料')
  wrapper.unmount()
})

it('材料页的引用进入目标会话草稿，刷新链接不会反复添加', async () => {
  vi.mocked(getMaterial).mockResolvedValue({ ...imageAttachment, created_at: '', excerpt: '', source_conversation_id: 'other' })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1?material=f1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  expect(getMaterial).toHaveBeenCalledWith('f1')
  expect(wrapper.get('[aria-label="待发送附件"]').text()).toContain('图表.png')
  expect(router.currentRoute.value.query.material).toBeUndefined()
  wrapper.unmount()
})

it('恢复后展示当前待审批步骤，执行后切换到下一张审批卡', async () => {
  const approval: Approval = {
    id: 'a1', run_id: 'r2', tool_call_id: 't1', tool_name: 'create_private_skill', tool_version: 1,
    risk_level: 'write', args_preview: { name: '复盘' }, status: 'pending', requested_at: '',
    expires_at: new Date(Date.now() + 60000).toISOString(), decided_at: null, decision_comment: null,
  }
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, current.message], has_more: false, active_run_id: 'r2' })
  vi.mocked(api.getRun).mockResolvedValue({ ...current, approval })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.get('[role="alertdialog"]').text()).toContain('创建私有技能草稿')
  const receive = vi.mocked(api.watchRun).mock.calls[0]![1]
  receive({ ...current, approval: { ...approval, status: 'executed' } })
  await flushPromises()
  expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
  receive({ ...current, approval: { ...approval, id: 'a2', tool_call_id: 't2', tool_name: 'publish_private_skill' } })
  await flushPromises()
  expect(wrapper.get('[role="alertdialog"]').text()).toContain('发布私有技能版本')
  wrapper.unmount()
})

it('输入区通过加号统一打开 Agent、能力和快捷指令，点击外部收起', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }, { path: '/materials', component: { template: '<div />' } }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  const opener = wrapper.get('[aria-label="更多功能"]')
  expect(opener.text()).toBe('+')
  expect(wrapper.find('[aria-label="选择 Agent"]').exists()).toBe(false)
  await opener.trigger('click')
  const menu = wrapper.get('[aria-label="输入框功能"]')
  expect(menu.find('[aria-label="选择 Agent"]').exists()).toBe(true)
  expect(menu.get('a[href="/materials"]').text()).toContain('我的材料')
  expect(menu.text()).toContain('整理成表格')
  await wrapper.get('textarea').trigger('pointerdown')
  expect(wrapper.find('[aria-label="输入框功能"]').exists()).toBe(false)
  await opener.trigger('click')
  await wrapper.get('.chat__capabilities').trigger('click')
  expect(wrapper.find('[aria-label="输入框功能"]').exists()).toBe(false)
  expect(wrapper.find('[aria-label="本次能力"]').exists()).toBe(true)
  wrapper.unmount()
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
  await wrapper.get('.chat-composer__stop').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('已停止')
  expect(wrapper.text()).toContain('部分回答 · 未完成')
  expect(api.cancelRun).toHaveBeenCalledWith('r2')
  wrapper.unmount()
})

it('右上角实时展示当前轮的计划和子 Agent，收起后继续轮询，旧轮记录仍可展开', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, old.message, current.message], has_more: false, active_run_id: 'r2' })
  const toolCalls = vi.mocked(getToolCalls)
  toolCalls.mockImplementation(async id => id === 'r1' ? executionCalls(id, 'completed', '旧轮计划') : [])
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  const top = wrapper.get('.chat__top')
  expect(top.find('[aria-label="本轮执行进度"]').exists()).toBe(false)
  const records = wrapper.findAll<HTMLDetailsElement>('.tool-calls')
  records[1]!.element.open = false
  await records[1]!.trigger('toggle')
  toolCalls.mockImplementation(async id => executionCalls(id, id === 'r1' ? 'completed' : 'running', id === 'r1' ? '旧轮计划' : '当前计划'))
  await vi.advanceTimersByTimeAsync(1000)
  const progress = top.get('[aria-label="本轮执行进度"]')
  const toggle = progress.get('button')
  expect(toggle.attributes('aria-expanded')).toBe('true')
  expect(progress.get('[aria-label="任务规划"]').text()).toContain('当前计划')
  expect(progress.get('[aria-label="子 Agent 协作"]').text()).toContain('执行中')
  expect(records[1]!.text()).not.toContain('当前计划')

  await toggle.trigger('click')
  toolCalls.mockImplementation(async id => executionCalls(id, 'completed', id === 'r1' ? '旧轮计划' : '当前计划'))
  await vi.advanceTimersByTimeAsync(1000)
  expect(toggle.attributes('aria-expanded')).toBe('false')
  expect(toggle.text()).toContain('规划 1/1')
  await toggle.trigger('click')
  expect(progress.get('[aria-label="子 Agent 协作"]').text()).toContain('已完成')
  vi.mocked(api.watchRun).mock.calls[0]![1]({
    run: { ...current.run, status: 'completed' }, message: { ...current.message, status: 'completed' },
  })
  await flushPromises()
  expect(toggle.attributes('aria-expanded')).toBe('true')
  records[0]!.element.open = true
  await records[0]!.trigger('toggle')
  await flushPromises()
  expect(records[0]!.text()).toContain('旧轮计划')
  expect(progress.text()).not.toContain('旧轮计划')
  await progress.get('[aria-label="任务规划"]').trigger('keydown', { key: 'Escape' })
  expect(toggle.attributes('aria-expanded')).toBe('false')
  expect(document.activeElement).toBe(toggle.element)

  vi.mocked(api.getMessages).mockResolvedValue({ items: [], has_more: false, active_run_id: null })
  await router.push('/chat/c2')
  await flushPromises()
  expect(top.find('[aria-label="本轮执行进度"]').exists()).toBe(false)
  wrapper.unmount()
})

it('刷新已结束的对话后，可以从右上角查看最后一轮的执行进度', async () => {
  vi.mocked(getToolCalls).mockResolvedValue(executionCalls('r1', 'failed', '已保存的计划'))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  const progress = wrapper.get('.chat__top [aria-label="本轮执行进度"]')
  const toggle = progress.get('button')
  expect(toggle.attributes('aria-expanded')).toBe('false')
  await toggle.trigger('click')
  expect(progress.text()).toContain('已保存的计划')
  expect(progress.get('[aria-label="子 Agent 协作"]').text()).toContain('失败')
  expect(getToolCalls).toHaveBeenCalledTimes(1)
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

it('引用侧栏以本条回答的运行记录为准，同编号不串号且未引用片段不计数', async () => {
  const previousAnswer = { ...old.message, content: '历史结论[1]，重复引用[1]，无法验证[3]。' }
  const latestAnswer = { ...current.message, content: '当前结论[1]。', status: 'completed' }
  const retrieval = (runId: string, name: string): ToolCall => ({
    id: runId, run_id: runId, tool_name: 'auto_knowledge_retrieval', status: 'completed',
    result: JSON.stringify({ chunks: [
      { index: 1, content: name + '原文', document_name: name },
      { index: 2, content: '未引用原文', document_name: '未引用文件' },
    ] }),
    days: null, error_code: null, created_at: '', finished_at: null,
  })
  vi.mocked(getToolCalls).mockImplementation(async id => [retrieval(id, id === 'r1' ? '历史制度' : '新制度')])
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, previousAnswer, latestAnswer], has_more: false, active_run_id: null })
  vi.mocked(api.getRun).mockResolvedValue({ run: { ...current.run, status: 'completed' }, message: latestAnswer })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  const [older, latest] = wrapper.findAll('.chat__message--assistant')
  expect(older!.get('details').element.open).toBe(false)
  expect(older!.text()).toContain('查看 1 条引用')
  expect(older!.find('button[data-cite="3"]').exists()).toBe(false)
  await older!.get('button[data-cite="1"]').trigger('click')
  const label = '[aria-label="引用来源"]'
  expect(wrapper.get(label).text()).toContain('历史制度原文')
  expect(wrapper.get(label).text()).not.toContain('未引用文件')
  await latest!.get('button[data-cite="1"]').trigger('click')
  expect(wrapper.get(label).text()).toContain('新制度原文')
  expect(wrapper.get(label).text()).not.toContain('历史制度原文')
  await wrapper.get(label).trigger('keydown', { key: 'Escape' })
  expect(wrapper.find(label).exists()).toBe(false)
  expect(document.activeElement).toBe(latest!.get('button[data-cite="1"]').element)
  await older!.get('button[data-cite="1"]').trigger('click')
  await router.push('/chat/c2')
  await flushPromises()
  expect(wrapper.find(label).exists()).toBe(false)
  wrapper.unmount()
})

it('追问将对应回答节选放进草稿，不覆盖草稿也不发送', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  const followUp = wrapper.findAll('button').find(button => button.text() === '基于此回答追问')!
  await wrapper.get('textarea').setValue('尚未发出的草稿')
  expect(followUp.element.disabled).toBe(true)
  await wrapper.get('textarea').setValue('')
  await followUp.trigger('click')
  expect(wrapper.get('textarea').element.value).toContain('旧回答')
  expect(api.createRun).not.toHaveBeenCalled()
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

it('失败后先恢复请求供确认，在同一对话发送时才重新执行', async () => {
  const failed: Snapshot = { run: { ...old.run, status: 'failed', error_message: '网络不可用' }, message: { ...old.message, status: 'interrupted' } }
  vi.mocked(api.getRun).mockResolvedValue(failed)
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, failed.message], has_more: false, active_run_id: null })
  vi.mocked(api.createRun).mockResolvedValue(current)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  const retry = wrapper.findAll('button').find(button => button.text() === '编辑后重试')
  expect(retry).toBeDefined()
  await wrapper.get('textarea').setValue('先保留这份草稿')
  expect(retry!.element.disabled).toBe(true)
  await wrapper.get('textarea').setValue('')
  await retry!.trigger('click')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('旧问题')
  expect(api.createRun).not.toHaveBeenCalled()
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(api.createRun).toHaveBeenCalledWith('c1', '旧问题', expect.any(String))
  expect(router.currentRoute.value.path).toBe('/chat/c1')
  wrapper.unmount()
})

it('草稿在会话间隔离，刷新后恢复，且不跨空间或账号显示', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const auth = useAuthStore()
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('textarea').setValue('会话一草稿')
  await router.push('/chat/c2')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('')
  await wrapper.get('textarea').setValue('会话二草稿')
  await router.push('/chat/c1')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('会话一草稿')
  wrapper.unmount()

  const resumed = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  expect(resumed.get('textarea').element.value).toBe('会话一草稿')
  auth.selectedWorkspaceId = 'w2'
  await flushPromises()
  expect(resumed.get('textarea').element.value).toBe('')
  await resumed.get('textarea').setValue('另一空间草稿')
  auth.session = { ...auth.session!, user: { ...auth.session!.user, id: 'u2' } }
  await flushPromises()
  expect(resumed.get('textarea').element.value).toBe('')
  resumed.unmount()
})

it('首次提交失败时把草稿保留在新建的对话内', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat')
  vi.mocked(api.createConversation).mockResolvedValue({ id: 'c2', title: '新对话', status: 'active', updated_at: '' })
  vi.mocked(api.getMessages).mockResolvedValue({ items: [], has_more: false, active_run_id: null })
  vi.mocked(api.createRun).mockRejectedValue(new Error('请求失败'))
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('textarea').setValue('新会话草稿')
  await wrapper.get('form').trigger('submit')
  await vi.waitFor(() => expect(api.createRun).toHaveBeenCalledTimes(1))
  expect(router.currentRoute.value.path).toBe('/chat/c2')
  expect(wrapper.get('textarea').element.value).toBe('新会话草稿')
  await router.push('/chat')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('')
  wrapper.unmount()
})

it('旧会话提交成功后不清除用户切换到的新会话草稿', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c2')
  let completeRun: ((value: Snapshot) => void) | undefined
  vi.mocked(api.createRun).mockImplementation(() => new Promise(resolve => { completeRun = resolve }))
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('textarea').setValue('另一会话的草稿')
  await router.push('/chat/c1')
  await flushPromises()
  await wrapper.get('textarea').setValue('已发送的问题')
  await wrapper.get('form').trigger('submit')
  await router.push('/chat/c2')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('另一会话的草稿')
  completeRun?.(current)
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('另一会话的草稿')
  await router.push('/chat/c1')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('')
  await router.push('/chat/c2')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('另一会话的草稿')
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

it('停止请求失败时不会自动发送补充要求', async () => {
  vi.mocked(api.getMessages).mockResolvedValue({ items: [first, current.message], has_more: false, active_run_id: 'r2' })
  vi.mocked(api.cancelRun).mockRejectedValue(new Error('停止请求失败'))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat/:conversationId?', component: ChatView }] })
  await router.push('/chat/c1')
  const wrapper = mount(ChatView, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('textarea').setValue('未发送的补充要求')
  const button = wrapper.findAll('button').find(item => item.text() === '停止并补充要求')
  await button!.trigger('click')
  await flushPromises()
  expect(api.createRun).not.toHaveBeenCalled()
  expect(wrapper.get('textarea').element.value).toBe('未发送的补充要求')
  expect(wrapper.text()).toContain('停止请求失败')
  wrapper.unmount()
})
