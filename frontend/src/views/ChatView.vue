<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { listAgents, type Agent } from '../api/agents'
import type { Approval } from '../api/approvals'
import AgentSelector from '../components/chat/AgentSelector.vue'
import ApprovalCard from '../components/chat/ApprovalCard.vue'
import CapabilityDrawer from '../components/chat/CapabilityDrawer.vue'
import ChunkSourceCard, { type SourceChunk } from '../components/chat/ChunkSourceCard.vue'
import MarkdownBody from '../components/MarkdownBody.vue'
import ToolCallList from '../components/ToolCallList.vue'
import { bindConversationAgent, cancelRun, createConversation, createRun, getMessages, getRun, isActive, listConversations, watchRun, type Conversation, type Message, type Run, type Snapshot } from '../api/chat'

const route = useRoute()
const router = useRouter()
const conversations = ref<Conversation[]>([])
const agents = ref<Agent[]>([])
const messages = ref<Message[]>([])
const input = ref('')
const run = ref<Run | null>(null)
const approval = ref<Approval | null>(null)
const selectedAgentId = ref<string | null>(null)
const drawerOpen = ref(false)
const loading = ref(false)
const submitting = ref(false)
const stopping = ref(false)
const error = ref('')
const connection = ref('')
const more = ref(false)
const transcript = ref<HTMLElement | null>(null)
const currentId = computed(() => typeof route.params.conversationId === 'string' ? route.params.conversationId : '')
const active = computed(() => run.value !== null && isActive(run.value))
const title = computed(() => conversations.value.find(item => item.id === currentId.value)?.title ?? '新对话')
const currentAgent = computed(() => agents.value.find(item => item.id === selectedAgentId.value) ?? null)
const prompts = computed(() => currentAgent.value?.starter_prompts.length
  ? currentAgent.value.starter_prompts
  : ['请帮我制定一个学习企业级 Agent 开发的实践计划', '查询最近七天的运行统计', '检索知识库并给出带来源的结论'])
const statuses: Record<string, string> = { pending: '等待生成', running: '正在生成', completed: '已完成', failed: '生成失败', cancelled: '已停止' }
let dispose: (() => void) | null = null
let revision = 0
let pending: { conversationId: string; content: string; key: string } | null = null
let alive = true

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
  if (value instanceof ApiError && value.requestId) error.value += ' · 请求标识：' + value.requestId
}
async function refreshList(append = false): Promise<void> {
  const items = await listConversations(append ? conversations.value.length : 0)
  conversations.value = append ? [...conversations.value, ...items] : items
  more.value = items.length === 50
  const found = conversations.value.find(item => item.id === currentId.value)
  if (found) selectedAgentId.value = found.agent_profile_id ?? null
}
async function loadAgents(): Promise<void> {
  try {
    agents.value = await listAgents()
    const saved = localStorage.getItem('ea-last-agent')
    if (!currentId.value && saved && agents.value.some(item => item.id === saved)) selectedAgentId.value = saved
  } catch (value) { showError(value) }
}
async function switchAgent(value: string | null): Promise<void> {
  if (active.value) return
  if (!currentId.value) {
    selectedAgentId.value = value
    if (value) localStorage.setItem('ea-last-agent', value)
    else localStorage.removeItem('ea-last-agent')
    return
  }
  if (selectedAgentId.value === value) return
  if (messages.value.length && !confirm('切换后仅影响后续回答，已有消息保留。继续切换？')) return
  try {
    const conversation = await bindConversationAgent(currentId.value, value)
    const index = conversations.value.findIndex(item => item.id === conversation.id)
    if (index >= 0) conversations.value[index] = conversation
    selectedAgentId.value = value
    if (value) localStorage.setItem('ea-last-agent', value)
    else localStorage.removeItem('ea-last-agent')
  } catch (value) { showError(value) }
}
async function applyDecision(updated: Approval): Promise<void> {
  approval.value = updated
  await reconnect()
}
function usePrompt(text: string): void { input.value = text }
const sourceChunks = ref<SourceChunk[]>([])
function handleAutoChunks(chunks: SourceChunk[]): void { sourceChunks.value = chunks }
function handleCite(index: number): void {
  const el = document.getElementById('source-' + index)
  if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.style.outline = '2px solid #18775c'; setTimeout(() => { el.style.outline = '' }, 2000) }
}
async function scrollToEnd(): Promise<void> {
  await nextTick()
  if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
}
function applySnapshot(snapshot: Snapshot): void {
  if (snapshot.run.conversation_id !== currentId.value) return
  run.value = snapshot.run
  approval.value = snapshot.approval ?? null
  const index = messages.value.findIndex(item => item.id === snapshot.message.id)
  if (index < 0) messages.value.push(snapshot.message)
  else messages.value[index] = snapshot.message
  if (!isActive(snapshot.run)) {
    dispose?.(); dispose = null
    void refreshList().catch(showError)
  }
  void scrollToEnd()
}
async function reconnect(): Promise<void> {
  if (!run.value) return
  const id = run.value.id
  dispose?.(); dispose = null
  try {
    const snapshot = await getRun(id)
    applySnapshot(snapshot)
    if (snapshot.run.conversation_id === currentId.value && isActive(snapshot.run)) {
      dispose = watchRun(id, applySnapshot, message => { connection.value = message })
    }
  } catch (value) { showError(value) }
}
async function loadConversation(): Promise<void> {
  const token = ++revision
  dispose?.(); dispose = null
  run.value = null; approval.value = null; messages.value = []; error.value = ''; connection.value = ''; pending = null
  const currentConversation = conversations.value.find(item => item.id === currentId.value)
  if (currentConversation) selectedAgentId.value = currentConversation.agent_profile_id ?? null
  if (!currentId.value) { loading.value = false; return }
  const id = currentId.value
  loading.value = true
  try {
    let after = 0
    while (alive && token === revision) {
      const page = await getMessages(id, after)
      if (!alive || token !== revision) return
      messages.value.push(...page.items)
      if (!page.has_more) {
        if (page.active_run_id) {
          const snapshot = await getRun(page.active_run_id)
          if (token !== revision || !alive) return
          applySnapshot(snapshot)
          if (isActive(snapshot.run)) dispose = watchRun(snapshot.run.id, applySnapshot, message => { connection.value = message })
        } else {
          const latest = [...messages.value].reverse().find(item => item.run_id)
          if (latest?.run_id) {
            const snapshot = await getRun(latest.run_id)
            if (token === revision && alive) applySnapshot(snapshot)
          }
        }
        break
      }
      const last = page.items.at(-1)
      if (!last || last.sequence <= after) throw new Error('历史消息分页异常')
      after = last.sequence
    }
    if (token === revision) await scrollToEnd()
  } catch (value) { if (token === revision && alive) showError(value) }
  finally { if (token === revision && alive) loading.value = false }
}
async function send(): Promise<void> {
  if (submitting.value || loading.value || active.value || !input.value.trim()) return
  submitting.value = true; error.value = ''; connection.value = ''
  const content = input.value
  try {
    let id = currentId.value
    if (!id) {
      const conversation = await createConversation('新对话', selectedAgentId.value)
      conversations.value.unshift(conversation)
      if (selectedAgentId.value) localStorage.setItem('ea-last-agent', selectedAgentId.value)
      await router.replace('/chat/' + conversation.id)
      // 等路由触发的历史加载完成，再向该对话提交，避免旧请求覆盖新消息。
      await nextTick()
      while (loading.value && alive) await new Promise(resolve => setTimeout(resolve, 20))
      id = conversation.id
    }
    if (!pending || pending.conversationId !== id || pending.content !== content) pending = { conversationId: id, content, key: crypto.randomUUID() }
    const snapshot = await createRun(id, content, pending.key)
    pending = null
    input.value = ''
    if (currentId.value === id && alive) {
      await loadConversation()
      applySnapshot(snapshot)
      await reconnect()
      await refreshList()
    }
  } catch (value) { showError(value) }
  finally { submitting.value = false }
}
async function stop(): Promise<void> {
  if (!run.value || stopping.value) return
  stopping.value = true; error.value = ''
  try { applySnapshot(await cancelRun(run.value.id)) } catch (value) { showError(value) }
  finally { stopping.value = false }
}
async function retry(): Promise<void> {
  const previous = [...messages.value].reverse().find(message => message.role === 'user')
  if (!previous || active.value) return
  input.value = previous.content
  await send()
}
async function supplement(): Promise<void> {
  await stop()
  if (!active.value) await send()
}
function keydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void send() }
}
watch(currentId, loadConversation, { immediate: true })
void refreshList().catch(showError)
void loadAgents()
onBeforeUnmount(() => { alive = false; revision++; dispose?.() })
</script>

<template>
  <div class="chat">
    <aside class="chat__history">
      <div class="chat__history-heading">
        <span>对话记录</span><button
          type="button"
          aria-label="刷新对话列表"
          @click="refreshList().catch(showError)"
        >
          ↻
        </button>
      </div>
      <button
        type="button"
        class="chat__new"
        :disabled="submitting"
        @click="router.push('/chat')"
      >
        ＋ 新对话
      </button>
      <nav aria-label="历史对话">
        <RouterLink
          v-for="item in conversations"
          :key="item.id"
          :to="'/chat/' + item.id"
          class="chat__conversation"
          :class="{ 'chat__conversation--active': item.id === currentId }"
        >{{ item.title }}</RouterLink>
      </nav>
      <p
        v-if="!conversations.length"
        class="chat__hint"
      >
        从第一个问题开始，<br />对话会自动保存在这里。
      </p>
      <button
        v-if="more"
        type="button"
        class="chat__link"
        @click="refreshList(true).catch(showError)"
      >
        加载更多
      </button>
      <span class="chat__local">私有对话 · 当前工作空间</span>
    </aside>
    <section
      class="chat__workspace"
      aria-label="问答工作区"
    >
      <div class="chat__top">
        <div><span class="chat__eyebrow">SEQUENCE / CONVERSATION</span><h1>{{ title }}</h1></div>
        <div class="chat__agent-bar">
          <AgentSelector
            :agents="agents"
            :model-value="selectedAgentId"
            :disabled="active"
            :current="currentAgent"
            @update:model-value="switchAgent"
          />
          <button
            type="button"
            class="chat__capabilities"
            @click="drawerOpen = !drawerOpen"
          >
            能力 {{ currentAgent?.tools.length ?? 0 }}
          </button>
          <span class="chat__model">{{ currentAgent?.model || 'DeepSeek' }}</span>
        </div>
      </div>
      <div class="chat__stage">
        <div
          ref="transcript"
          class="chat__transcript"
          :aria-busy="loading"
        >
          <div
            v-if="loading"
            class="chat__hint"
          >
            正在读取对话…
          </div>
          <div
            v-else-if="!messages.length"
            class="chat__welcome"
          >
            <span class="chat__symbol">≋</span><h2>从一个问题，<br />打开新的思路。</h2><p>当前使用「{{ currentAgent?.name ?? '序列助手' }}」。<br />直接提问，或交给我一个复杂目标；我会按配置使用工具、技能和知识库。</p>
            <div class="chat__prompts">
              <button
                v-for="prompt in prompts"
                :key="prompt"
                type="button"
                @click="usePrompt(prompt)"
              >
                {{ prompt }} ↗
              </button>
            </div>
          </div>
          <article
            v-for="message in messages"
            :key="message.id"
            class="chat__message"
            :class="'chat__message--' + message.role"
          >
            <div class="chat__speaker">
              {{ message.role === 'user' ? '你' : '序列' }}<span v-if="message.status === 'streaming'">正在思考与生成</span><span v-else-if="message.status === 'interrupted'">部分回答 · 未完成</span>
            </div>
            <MarkdownBody
              class="chat__body"
              :content="message.content || (message.status === 'streaming' ? '正在处理；规划与执行情况见下方…' : '本次未生成正文')"
              :citations="message.role === 'assistant'"
              @cite="handleCite"
            />
            <ChunkSourceCard
              v-if="message.role === 'assistant' && sourceChunks.length"
              :chunks="sourceChunks"
              :collapsible="true"
            />
            <ToolCallList
              v-if="message.run_id"
              :run-id="message.run_id"
              :message-status="message.status"
              @auto-chunks="handleAutoChunks"
            />
          </article>
        </div>
        <CapabilityDrawer
          :agent="currentAgent"
          :open="drawerOpen"
          @close="drawerOpen = false"
        />
      </div>
      <div class="chat__composer">
        <ApprovalCard
          v-if="approval && run"
          :approval="approval"
          :run-id="run.id"
          @decided="applyDecision"
        />
        <div
          v-if="error"
          class="chat__error"
          role="alert"
        >
          {{ error }}
        </div>
        <div
          v-if="run"
          class="chat__run"
          role="status"
        >
          <span>{{ statuses[run.status] }}<template v-if="run.error_message"> · {{ run.error_message }}</template></span><button
            v-if="run.status === 'failed' || run.status === 'cancelled'"
            type="button"
            class="chat__link"
            :disabled="submitting"
            @click="retry"
          >
            重试本次请求
          </button><code>{{ run.id }}</code>
        </div>
        <div
          v-if="connection"
          class="chat__connection"
        >
          {{ connection }} <button
            type="button"
            class="chat__link"
            @click="reconnect"
          >
            重新连接
          </button>
        </div>
        <form
          class="chat__form"
          @submit.prevent="send"
        >
          <label
            class="chat__sr-only"
            for="question"
          >输入问题</label><textarea
            id="question"
            v-model="input"
            placeholder="输入问题或任务目标，也可以补充要求…"
            maxlength="20000"
            :disabled="loading || submitting"
            rows="3"
            @keydown="keydown"
          />
          <div class="chat__actions">
            <span>Enter 发送 · Shift + Enter 换行</span><button
              v-if="active"
              type="button"
              class="chat__stop"
              :disabled="stopping"
              @click="stop"
            >
              {{ stopping ? '正在停止…' : '■ 停止生成' }}
            </button><button
              v-if="active && input.trim()"
              type="button"
              class="chat__send"
              :disabled="stopping || submitting"
              @click="supplement"
            >
              停止并补充要求
            </button><button
              v-if="!active"
              type="submit"
              class="chat__send"
              :disabled="!input.trim() || loading || submitting"
            >
              {{ submitting ? '正在提交…' : '发送 ↑' }}
            </button>
          </div>
        </form>
        <p class="chat__disclaimer">
          回答由 AI 生成，请核实重要信息。对话保存在本地数据库。
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.chat {
  display: grid; grid-template-columns: 235px minmax(0, 1fr); height: calc(100dvh - 77px); min-height: 540px;
  &__history { background: #f2f4ed; border-right: 1px solid $line; padding: 26px 16px; display: flex; flex-direction: column; gap: 20px; overflow: auto; }
  &__history-heading { display: flex; justify-content: space-between; align-items: center; padding: 0 8px; color: $muted; font-size: 12px; button { background: none; border: 0; font-size: 20px; color: $green; } }
  &__new { border: 1px solid #cad6bf; color: $green; background: #e8eedd; padding: 12px; border-radius: 6px; text-align: left; }
  &__conversation { display: block; padding: 13px 11px; color: #657361; font-size: 12px; line-height: 1.5; border-radius: 6px; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; &--active { background: #dfe8d5; color: #294d38; } &:hover { background: #e6ecde; } }
  &__local { margin-top: auto; color: #87927e; font-size: 10px; padding: 20px 8px 0; }
  &__workspace { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
  &__top { padding: 25px 7%; display: flex; align-items: center; justify-content: space-between; gap: 20px; border-bottom: 1px solid $line; h1 { font-size: 17px; font-weight: 500; margin-top: 9px; overflow-wrap: anywhere; } }
  &__agent-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
  &__capabilities { border: 1px solid #cbd7be; background: #eef2e7; color: $green; border-radius: 20px; padding: 7px 11px; font-size: 11px; }
  &__stage { flex: 1; display: flex; min-height: 0; min-width: 0; }
  &__eyebrow { font-size: 9px; letter-spacing: 2px; color: #859576; }
  &__model { font-size: 10px; color: $green; border: 1px solid #cbd7be; border-radius: 20px; padding: 7px 12px; }
  &__transcript { flex: 1; overflow-y: auto; padding: 25px max(24px, 9%); scroll-behavior: smooth; }
  &__welcome { padding: 30px 0 40px; h2 { font-size: clamp(25px, 3vw, 40px); font-weight: 400; line-height: 1.6; letter-spacing: -1px; margin: 20px 0; } p { line-height: 2; color: $muted; font-size: 12px; } }
  &__prompts { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 26px; button { background: #eef2e7; border: 1px solid $line; border-radius: 6px; padding: 11px 15px; color: #516b42; font-size: 12px; text-align: left; } }
  &__symbol { font-size: 48px; color: #718e57; }
  &__message { padding: 22px 0 30px; border-bottom: 1px solid #e5eadf; margin-bottom: 10px; &--user .chat__body { background: #eaf0e1; padding: 15px 18px; border-radius: 8px; } }
  &__speaker { font-size: 12px; color: $green; margin-bottom: 13px; display: flex; align-items: center; gap: 16px; span { font-size: 10px; color: $muted; } }
  &__body { overflow-wrap: anywhere; font-size: 14px; line-height: 1.95; }
  &__composer { padding: 15px max(24px, 7%) 18px; background: $paper; }
  &__form { background: white; border: 1px solid #c7d5bc; border-radius: 10px; box-shadow: 0 5px 20px #24391c06; padding: 14px; &:focus-within { border-color: #76925f; } textarea { display: block; width: 100%; border: 0; outline: 0; resize: vertical; max-height: 220px; min-height: 55px; font: inherit; color: $ink; line-height: 1.8; background: transparent; } }
  &__actions { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding-top: 12px; span { font-size: 10px; color: #8b9683; } }
  &__send, &__stop { background: #31533d; color: #edf4e4; border: 0; border-radius: 6px; padding: 10px 18px; font-size: 12px; }
  &__stop { background: #f2eadc; color: #7a613f; }
  &__disclaimer { text-align: center; color: #8b9683; font-size: 10px; margin-top: 10px; }
  &__error { color: #8b493c; background: #faf0e8; border: 1px solid #e8d7c7; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-bottom: 10px; overflow-wrap: anywhere; }
  &__run { display: flex; justify-content: space-between; gap: 12px; color: #748067; font-size: 10px; margin-bottom: 8px; flex-wrap: wrap; code { font-size: 9px; } }
  &__connection, &__hint { font-size: 12px; color: $muted; line-height: 1.9; }
  &__link { background: none; border: 0; color: $green; text-decoration: underline; padding: 5px; font-size: 12px; }
  &__sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
}
@media (max-width: 1100px) { .chat { grid-template-columns: 180px minmax(0, 1fr); &__top { padding: 22px; } } }
@media (max-width: 760px) { .chat { grid-template-columns: minmax(0, 1fr); height: auto; min-height: calc(100dvh - 170px); &__history { max-height: 190px; border-right: 0; gap: 10px; padding: 14px; nav { display: flex; overflow-x: auto; } } &__conversation { max-width: 180px; flex-shrink: 0; } &__history-heading, &__local { display: none; } &__transcript { min-height: 250px; max-height: 60dvh; } &__welcome { padding: 0; } &__actions span { font-size: 9px; } } }
@media (prefers-reduced-motion: reduce) { .chat__transcript { scroll-behavior: auto; } }
</style>
