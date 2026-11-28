<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { listAgents, type Agent } from '../api/agents'
import type { Approval } from '../api/approvals'
import AgentSelector from '../components/chat/AgentSelector.vue'
import AnswerMessage from '../components/chat/AnswerMessage.vue'
import ApprovalCard from '../components/chat/ApprovalCard.vue'
import CapabilityDrawer from '../components/chat/CapabilityDrawer.vue'
import ChatComposer from '../components/chat/ChatComposer.vue'
import ConnectorConfigDialog from './connectors/ConnectorConfigDialog.vue'
import ChatFileCard from '../components/chat/ChatFileCard.vue'
import type { SourceChunk } from '../components/chat/ChunkSourceCard.vue'
import SourceDrawer from '../components/chat/SourceDrawer.vue'
import MarkdownBody from '../components/MarkdownBody.vue'
import { bindConversationAgent, cancelRun, createConversation, createRun, createRunWithFiles, getMessages, getRun, isActive, listConversations, watchRun, type Conversation, type Message, type Run, type Snapshot } from '../api/chat'
import { attachmentAccept, attachmentOnlyQuestion, chatAttachmentUrl, isAttachmentImage, maxAttachmentBytes, maxAttachmentCount, maxTotalAttachmentBytes, type ChatAttachment } from '../api/chatAttachments'
import { getMaterial, materialFileUrl } from '../api/materials'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const conversations = ref<Conversation[]>([])
const agents = ref<Agent[]>([])
const messages = ref<Message[]>([])
const input = ref('')
interface DraftFile { key: string; filename: string; size: number; preview?: string; file?: File; savedId?: string }
const fileDraft = shallowRef<DraftFile[]>([])
const draftFileIds = computed(() => new Set(fileDraft.value.flatMap(item => item.savedId ? [item.savedId] : [])))
const fileDrafts = new Map<string, DraftFile[]>()
const fileExtensions = new Set(attachmentAccept.split(','))
const run = ref<Run | null>(null)
const approval = ref<Approval | null>(null)
const selectedAgentId = ref<string | null>(null)
const drawerOpen = ref(false)
const mailDialogOpen = ref(false)
const meetingQuestion = '请在我的 QQ 邮箱里找到最近一封项目会议通知，读取正文并告诉我会议时间。'
const loading = ref(false)
const submitting = ref(false)
const stopping = ref(false)
const switchingAgent = ref(false)
const error = ref('')
const connection = ref('')
const more = ref(false)
const transcript = ref<HTMLElement | null>(null)
const progressTarget = ref<HTMLElement | null>(null)
const composer = ref<InstanceType<typeof ChatComposer> | null>(null)
const currentId = computed(() => typeof route.params.conversationId === 'string' ? route.params.conversationId : '')
const draftScope = computed(() => auth.session?.user.id && auth.selectedWorkspaceId
  ? [auth.session.user.id, auth.selectedWorkspaceId] : null)
const draftKey = computed(() => getDraftKey(currentId.value))
const fileDraftKey = computed(() => draftKey.value || currentId.value)
const sendModeKey = computed(() => auth.session?.user.id ? 'ea-chat-send-mode:' + auth.session.user.id : '')
const sendMode = ref<'enter' | 'modifier'>('enter')
const draftFallback = new Map<string, string>()
const active = computed(() => run.value !== null && isActive(run.value))
const title = computed(() => conversations.value.find(item => item.id === currentId.value)?.title ?? '新对话')
const currentAgent = computed(() => agents.value.find(item => item.id === selectedAgentId.value) ?? null)
const retryable = computed(() => run.value?.status === 'failed' || run.value?.status === 'cancelled')
const prompts = computed(() => currentAgent.value?.starter_prompts.length
  ? currentAgent.value.starter_prompts
  : ['请帮我制定一个学习企业级 Agent 开发的实践计划', '查询最近七天的运行统计', '检索知识库并给出带来源的结论'])
const statuses: Record<string, string> = { pending: '等待生成', running: '正在生成', completed: '已完成', failed: '生成失败', cancelled: '已停止' }
let dispose: (() => void) | null = null
let revision = 0
let reconnectAttempt = 0
let pending: { conversationId: string; content: string; fileKeys: string; key: string } | null = null
let alive = true

function getDraftKey(id: string): string {
  return draftScope.value ? 'ea-chat-draft:' + JSON.stringify([...draftScope.value, id]) : ''
}

function getDraft(key: string): string {
  if (!key) return ''
  const fallback = draftFallback.get(key)
  if (fallback !== undefined) return fallback
  try { return sessionStorage.getItem(key) ?? '' }
  catch { return '' }
}

function writeDraft(value: string, key = draftKey.value): void {
  if (key === draftKey.value) input.value = value
  if (!key) return
  try {
    if (value) sessionStorage.setItem(key, value)
    else sessionStorage.removeItem(key)
    draftFallback.delete(key)
  } catch { draftFallback.set(key, value) }
}

function writeFileDraft(items: DraftFile[], key = fileDraftKey.value): void {
  if (key === fileDraftKey.value) fileDraft.value = items
  if (items.length) fileDrafts.set(key, items)
  else fileDrafts.delete(key)
}

function releaseFile(item: DraftFile): void {
  if (item.file && item.preview) URL.revokeObjectURL(item.preview)
}

function fitsFileDraft(size: number, items: DraftFile[]): boolean {
  if (items.length < maxAttachmentCount && items.reduce((total, item) => total + item.size, size) <= maxTotalAttachmentBytes) return true
  error.value = '每条消息最多 4 个附件，合计最多 16 MB'
  return false
}

function addFiles(files: File[]): void {
  if (submitting.value || loading.value) return
  error.value = ''
  const items = [...fileDraft.value]
  for (const file of files) {
    const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
    if (!fileExtensions.has(extension)) { error.value = `暂不支持「${file.name}」的格式`; continue }
    if (!file.size || file.size > maxAttachmentBytes) { error.value = '附件不能为空，单个文件最多 8 MB'; continue }
    if (!fitsFileDraft(file.size, items)) break
    if (items.some(item => item.file?.name === file.name && item.size === file.size
      && item.file.lastModified === file.lastModified)) continue
    const preview = isAttachmentImage(file.type) && typeof URL.createObjectURL === 'function'
      ? URL.createObjectURL(file) : undefined
    items.push({ key: crypto.randomUUID(), filename: file.name, size: file.size, file, preview })
  }
  writeFileDraft(items)
}

function removeFile(item: DraftFile): void {
  if (submitting.value) return
  releaseFile(item)
  writeFileDraft(fileDraft.value.filter(entry => entry.key !== item.key))
}

function reuseFile(item: ChatAttachment, conversationId?: string): void {
  if (submitting.value || (conversationId !== undefined && currentId.value !== conversationId)
    || fileDraft.value.some(entry => entry.savedId === item.id)) return
  if (!fitsFileDraft(item.size_bytes, fileDraft.value)) return
  error.value = ''
  writeFileDraft([...fileDraft.value, {
    key: item.id, savedId: item.id, filename: item.filename, size: item.size_bytes,
    preview: isAttachmentImage(item.media_type) ? materialFileUrl(item.id) : undefined,
  }])
}

function setSendMode(mode: 'enter' | 'modifier'): void {
  sendMode.value = mode
  if (!sendModeKey.value) return
  try { localStorage.setItem(sendModeKey.value, mode) } catch { return }
}

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
  if (active.value || loading.value || submitting.value || switchingAgent.value) return
  if (!currentId.value) {
    selectedAgentId.value = value
    if (value) localStorage.setItem('ea-last-agent', value)
    else localStorage.removeItem('ea-last-agent')
    return
  }
  if (selectedAgentId.value === value) return
  if (messages.value.length && !confirm('切换后仅影响后续回答，已有消息保留。继续切换？')) return
  const id = currentId.value
  switchingAgent.value = true
  try {
    const conversation = await bindConversationAgent(id, value)
    if (!alive) return
    const index = conversations.value.findIndex(item => item.id === conversation.id)
    if (index >= 0) conversations.value[index] = conversation
    if (id !== currentId.value) return
    selectedAgentId.value = value
    if (value) localStorage.setItem('ea-last-agent', value)
    else localStorage.removeItem('ea-last-agent')
  } catch (value) { if (alive && id === currentId.value) showError(value) }
  finally { switchingAgent.value = false }
}
async function applyDecision(updated: Approval): Promise<void> {
  if (run.value?.id !== updated.run_id || approval.value?.id !== updated.id) return
  approval.value = updated
  await reconnect()
}
function usePrompt(text: string): void {
  writeDraft(text)
  void composer.value?.focus()
}
async function resumeMailQuestion(): Promise<void> {
  mailDialogOpen.value = false
  if (!input.value.trim()) writeDraft(meetingQuestion)
  await nextTick()
  void composer.value?.focus()
}
const sourceSelection = ref<{ chunks: SourceChunk[]; index: number | null } | null>(null)
let sourceTrigger: HTMLElement | null = null
function closeSources(restoreFocus = true): void {
  sourceSelection.value = null
  if (restoreFocus && sourceTrigger?.isConnected) sourceTrigger.focus({ preventScroll: true })
  sourceTrigger = null
}
function openSources(chunks: SourceChunk[], index: number | null, trigger: HTMLElement): void {
  drawerOpen.value = false
  sourceTrigger = trigger
  sourceSelection.value = { chunks, index }
}
function toggleCapabilities(): void {
  closeSources(false)
  drawerOpen.value = !drawerOpen.value
}
function followUp(message: Message): void {
  if (input.value.trim() || loading.value || submitting.value || switchingAgent.value) return
  const context = message.content.trim() || message.reports?.map(report => report.title).join('、') || ''
  const excerpt = context.replace(/\s+/g, ' ').trim()
  const limit = 180
  if (!excerpt) return
  usePrompt('关于这条回答「' + excerpt.slice(0, limit) + (excerpt.length > limit ? '…' : '') + '」，我想追问：')
}
async function scrollToEnd(): Promise<void> {
  await nextTick()
  if (transcript.value && !sourceSelection.value) transcript.value.scrollTop = transcript.value.scrollHeight
}
function applySnapshot(snapshot: Snapshot): void {
  if (snapshot.run.conversation_id !== currentId.value) return
  run.value = snapshot.run
  connection.value = ''
  approval.value = snapshot.approval ?? null
  const index = messages.value.findIndex(item => item.id === snapshot.message.id)
  if (index < 0) messages.value.push(snapshot.message)
  else {
    // 报表一旦挂到消息上就会保留，较早的快照可能尚未携带这份引用。
    const reports = new Map(messages.value[index]?.reports?.map(report => [report.id, report]))
    for (const report of snapshot.message.reports ?? []) reports.set(report.id, report)
    messages.value[index] = reports.size ? { ...snapshot.message, reports: [...reports.values()] } : snapshot.message
  }
  if (!isActive(snapshot.run)) {
    dispose?.(); dispose = null
    void refreshList().catch(showError)
  }
  void scrollToEnd()
}
async function reconnect(): Promise<void> {
  if (!run.value || !active.value) return
  const id = run.value.id
  const context = revision
  const attempt = ++reconnectAttempt
  // 断线查询可能晚于停止、切换会话或下一次重连返回。
  const isCurrent = () => alive && revision === context && reconnectAttempt === attempt && run.value?.id === id && active.value
  dispose?.(); dispose = null
  try {
    const snapshot = await getRun(id)
    if (!isCurrent()) return
    applySnapshot(snapshot)
    if (isActive(snapshot.run)) {
      dispose = watchRun(id, applySnapshot, message => { connection.value = message })
    }
  } catch (value) {
    if (isCurrent()) connection.value = value instanceof Error ? value.message : '未能恢复回答，请重新连接'
  }
}
async function loadConversation(): Promise<void> {
  const token = ++revision
  closeSources(false)
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
  if (submitting.value || loading.value || stopping.value || switchingAgent.value || active.value
    || (!input.value.trim() && !fileDraft.value.length) || input.value === '/') return
  submitting.value = true; error.value = ''; connection.value = ''
  const original = input.value
  const content = original.trim() ? original : attachmentOnlyQuestion
  const attachments = fileDraft.value
  const fileKeys = attachments.map(item => item.key).join(',')
  let submissionKey = draftKey.value
  let uploadKey = fileDraftKey.value
  let id = currentId.value
  try {
    if (!id) {
      const conversation = await createConversation('新对话', selectedAgentId.value)
      if (!alive || id !== currentId.value || submissionKey !== draftKey.value) return
      conversations.value.unshift(conversation)
      const nextKey = getDraftKey(conversation.id)
      if (await router.replace('/chat/' + conversation.id) || !alive || currentId.value !== conversation.id || draftKey.value !== nextKey) return
      writeDraft(original, nextKey)
      if (submissionKey && submissionKey !== nextKey) writeDraft('', submissionKey)
      submissionKey = nextKey
      const migratedUploadKey = nextKey || conversation.id
      writeFileDraft(attachments, migratedUploadKey)
      if (uploadKey !== migratedUploadKey) writeFileDraft([], uploadKey)
      uploadKey = migratedUploadKey
      id = conversation.id
      // 等路由触发的历史加载完成，再向该对话提交，避免旧请求覆盖新消息。
      await nextTick()
      while (loading.value && alive && currentId.value === id && draftKey.value === submissionKey) {
        await new Promise(resolve => setTimeout(resolve, 20))
      }
    }
    if (!alive || currentId.value !== id || draftKey.value !== submissionKey) return
    if (!pending || pending.conversationId !== id || pending.content !== content || pending.fileKeys !== fileKeys) {
      pending = { conversationId: id, content, fileKeys, key: crypto.randomUUID() }
    }
    const attempt = pending
    const files = attachments.flatMap(item => item.file ? [item.file] : [])
    const refs = attachments.flatMap(item => item.savedId ? [item.savedId] : [])
    const snapshot = files.length ? await createRunWithFiles(id, content, attempt.key, files, refs)
      : refs.length ? await createRun(id, content, attempt.key, refs) : await createRun(id, content, attempt.key)
    if (pending === attempt) pending = null
    if (submissionKey ? getDraft(submissionKey) === original : currentId.value === id && input.value === original) writeDraft('', submissionKey)
    if (alive && fileDrafts.get(uploadKey) === attachments) {
      attachments.forEach(releaseFile)
      writeFileDraft([], uploadKey)
    }
    if (currentId.value === id && draftKey.value === submissionKey && alive) {
      await loadConversation()
      if (!alive || currentId.value !== id || draftKey.value !== submissionKey) return
      // 历史加载已经恢复最新正文及订阅，仅在历史缺少本轮时用提交回执恢复。
      const latest = messages.value.find(message => message.run_id && message.run_id === run.value?.id)
      if (!latest || latest.sequence < snapshot.message.sequence) {
        applySnapshot(snapshot)
        if (isActive(snapshot.run)) await reconnect()
      }
      await refreshList()
    }
  } catch (value) { if (alive && currentId.value === id && draftKey.value === submissionKey) showError(value) }
  finally { submitting.value = false }
}
async function stop(): Promise<boolean> {
  const target = run.value
  if (!target || !active.value || stopping.value) return false
  const context = draftKey.value
  stopping.value = true; error.value = ''
  try {
    const snapshot = await cancelRun(target.id)
    if (!alive || draftKey.value !== context || run.value?.id !== target.id) return false
    applySnapshot(snapshot)
    return !isActive(snapshot.run)
  } catch (value) {
    if (alive && draftKey.value === context && run.value?.id === target.id) showError(value)
    return false
  }
  finally { stopping.value = false }
}
function retry(): void {
  if (!retryable.value || input.value.trim() || fileDraft.value.length || submitting.value) return
  const previous = [...messages.value].reverse().find(message => message.role === 'user')
  if (previous) {
    usePrompt(previous.content)
    for (const item of previous.attachments ?? []) reuseFile(item, previous.conversation_id)
  }
}
async function supplement(): Promise<void> {
  if (await stop()) await send()
}
watch(draftKey, key => { input.value = getDraft(key) }, { immediate: true, flush: 'sync' })
watch(fileDraftKey, key => { fileDraft.value = fileDrafts.get(key) ?? [] }, { immediate: true, flush: 'sync' })
watch(sendModeKey, key => {
  try { sendMode.value = key && localStorage.getItem(key) === 'modifier' ? 'modifier' : 'enter' }
  catch { sendMode.value = 'enter' }
}, { immediate: true })
watch(currentId, loadConversation, { immediate: true })
watch(fileDraftKey, () => { mailDialogOpen.value = false }, { flush: 'sync' })
watch(() => route.query.plugin, async (key) => {
  if (key !== 'qq_mail') return
  const context = fileDraftKey.value
  if (!input.value.trim()) writeDraft(meetingQuestion)
  await router.replace({ query: { ...route.query, plugin: undefined } })
  await nextTick()
  if (alive && fileDraftKey.value === context) void composer.value?.focus()
}, { immediate: true })
watch(() => route.query.material, async (id) => {
  if (typeof id !== 'string' || !id) return
  const context = fileDraftKey.value
  try {
    const item = await getMaterial(id)
    if (!alive || fileDraftKey.value !== context || route.query.material !== id) return
    reuseFile(item)
    void composer.value?.focus()
  } catch (value) {
    if (alive && fileDraftKey.value === context && route.query.material === id) showError(value)
  } finally {
    if (alive && fileDraftKey.value === context && route.query.material === id) {
      await router.replace({ query: { ...route.query, material: undefined } })
    }
  }
}, { immediate: true })
void refreshList().catch(showError)
void loadAgents()
onBeforeUnmount(() => {
  alive = false; revision++; dispose?.()
  for (const items of fileDrafts.values()) items.forEach(releaseFile)
  fileDrafts.clear()
})
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
        <h1>{{ title }}</h1>
        <div class="chat__top-actions">
          <span
            v-if="run || currentAgent"
            class="chat__model"
          >{{ run?.model ?? currentAgent?.model }}</span>
          <div ref="progressTarget" />
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
            <span
              class="chat__symbol"
              aria-hidden="true"
            >≋</span>
            <h2>从一个问题开始</h2>
            <p>当前使用「{{ currentAgent?.name ?? '序列助手' }}」。可以直接提问，也可以描述要完成的任务。</p>
            <div class="chat__prompts">
              <button
                type="button"
                :disabled="submitting || active"
                @click="mailDialogOpen = true"
              >
                查邮箱里的会议通知 ↗
              </button>
              <button
                v-for="prompt in prompts"
                :key="prompt"
                type="button"
                :disabled="Boolean(input.trim())"
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
            <template v-if="message.role === 'user'">
              <div class="chat__speaker">
                你
              </div>
              <MarkdownBody
                class="chat__body"
                :content="message.content"
              />
            </template>
            <AnswerMessage
              v-else
              :message="message"
              :run="run && run.id === message.run_id ? run : undefined"
              :progress-target="run && run.id === message.run_id ? progressTarget : null"
              :status-label="run && run.id === message.run_id ? statuses[run.status] : undefined"
              :follow-up-disabled="loading || submitting || switchingAgent || Boolean(input.trim())"
              :reuse-files-disabled="submitting || loading"
              :draft-file-ids="draftFileIds"
              @sources="openSources"
              @follow-up="followUp(message)"
              @reuse-file="reuseFile($event, message.conversation_id); composer?.focus()"
            >
              <template
                v-if="retryable && run?.id === message.run_id"
                #actions
              >
                <button
                  type="button"
                  :disabled="submitting || loading || Boolean(input.trim()) || !!fileDraft.length"
                  :title="input.trim() || fileDraft.length ? '请先处理当前草稿' : '将原问题和附件带回草稿'"
                  @click="retry"
                >
                  编辑后重试
                </button>
              </template>
            </AnswerMessage>
            <div
              v-if="message.role === 'user' && message.attachments?.length"
              class="chat__files"
              aria-label="消息附件"
            >
              <ChatFileCard
                v-for="item in message.attachments"
                :key="item.id"
                :filename="item.filename"
                :size="item.size_bytes"
                :preview="isAttachmentImage(item.media_type) ? chatAttachmentUrl(message.conversation_id, item.id) : undefined"
                :download="chatAttachmentUrl(message.conversation_id, item.id, true)"
                :note="item.text_truncated ? '已读取部分文字' : !item.text_available && !isAttachmentImage(item.media_type) ? '未提取到文字' : undefined"
              >
                <button
                  type="button"
                  :disabled="submitting || loading || draftFileIds.has(item.id)"
                  :aria-label="'再次附加 ' + item.filename"
                  @click="reuseFile(item, message.conversation_id); composer?.focus()"
                >
                  再次附加
                </button>
              </ChatFileCard>
            </div>
          </article>
        </div>
        <CapabilityDrawer
          :agent="currentAgent"
          :open="drawerOpen"
          @close="drawerOpen = false"
        />
        <SourceDrawer
          v-if="sourceSelection"
          :chunks="sourceSelection.chunks"
          :index="sourceSelection.index"
          @close="closeSources()"
        />
      </div>
      <div class="chat__composer">
        <ApprovalCard
          v-if="approval?.status === 'pending' && run"
          :key="approval.id"
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
          v-if="run?.error_message"
          class="chat__run"
          role="status"
        >
          {{ statuses[run.status] }} · {{ run.error_message }}
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
        <ChatComposer
          ref="composer"
          :model-value="input"
          :context-key="draftKey || currentId"
          :active="active"
          :loading="loading || switchingAgent"
          :submitting="submitting"
          :stopping="stopping"
          :send-mode="sendMode"
          :attachment-count="fileDraft.length"
          @update:model-value="writeDraft"
          @update:send-mode="setSendMode"
          @send="send"
          @stop="stop"
          @supplement="supplement"
          @attach="addFiles"
        >
          <template #attachments>
            <div
              v-if="fileDraft.length"
              class="chat__files chat__files--draft"
              aria-label="待发送附件"
            >
              <ChatFileCard
                v-for="item in fileDraft"
                :key="item.key"
                :filename="item.filename"
                :size="item.size"
                :preview="item.preview"
                :note="submitting ? '提交中…' : undefined"
              >
                <button
                  type="button"
                  :disabled="submitting"
                  :aria-label="'移除 ' + item.filename"
                  @click="removeFile(item)"
                >
                  移除
                </button>
              </ChatFileCard>
            </div>
          </template>
          <template #tools="{ close }">
            <button
              type="button"
              class="chat__mail-shortcut"
              :disabled="active || loading || submitting"
              @click="close(); mailDialogOpen = true"
            >
              查邮箱里的会议通知
            </button>
            <RouterLink
              class="chat__material-link"
              to="/materials"
              @click="close()"
            >
              我的材料 ↗
            </RouterLink>
            <AgentSelector
              :agents="agents"
              :model-value="selectedAgentId"
              :disabled="active || loading || submitting || switchingAgent"
              :current="currentAgent"
              @update:model-value="switchAgent"
            />
            <button
              type="button"
              class="chat__capabilities"
              :aria-expanded="drawerOpen"
              @click="toggleCapabilities(); close()"
            >
              查看能力
            </button>
          </template>
        </ChatComposer>
        <p class="chat__disclaimer">
          回答由 AI 生成，请核实重要信息。对话保存在本地数据库。
        </p>
      </div>
    </section>
    <ConnectorConfigDialog
      v-if="mailDialogOpen"
      connector-key="qq_mail"
      member-mode
      @close="mailDialogOpen = false; composer?.focus()"
      @mail-ready="resumeMailQuestion"
    />
  </div>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.chat {
  display: grid; grid-template-columns: 216px minmax(0, 1fr); height: calc(100dvh - #{$header-height}); min-height: 540px;
  &__history { background: $paper; border-right: 1px solid $line; padding: 17px 13px; display: flex; flex-direction: column; gap: 12px; overflow: auto; }
  &__history-heading {
    display: flex; justify-content: space-between; align-items: center; padding: 0 6px; color: $muted; font-size: 12px;
    button { background: none; border: 0; font-size: 20px; color: $green; }
  }
  &__new {
    border: 1px solid $line; color: $green; background: $surface; padding: 8px 11px;
    border-radius: $control-radius; text-align: left; font-weight: 650;
    &:enabled:hover { border-color: $green; }
  }
  &__conversation {
    display: block; padding: 9px 10px; color: $muted; font-size: 13px; line-height: 1.5; border-radius: $control-radius;
    text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    &:hover { background: $sidebar; color: $ink; }
    &--active, &--active:hover { background: $selection; color: $green; font-weight: 650; }
  }
  &__local { margin-top: auto; color: $muted; font-size: 11px; padding: 16px 6px 0; }
  &__workspace { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
  &__top {
    padding: 13px clamp(16px, 4vw, 42px); display: flex; align-items: center; justify-content: space-between;
    gap: 14px; min-height: 57px; border-bottom: 1px solid $line; background: $surface;
    h1 { font-size: 17px; font-weight: 650; overflow-wrap: anywhere; min-width: 0; }
  }
  &__top-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 10px; }
  &__top-actions > :empty { display: none; }
  &__stage { flex: 1; display: flex; min-height: 0; min-width: 0; }
  &__model { font-size: 11px; color: $green; background: $sidebar; border-radius: 5px; padding: 4px 8px; }
  &__transcript { flex: 1; min-width: 0; overflow-y: auto; padding: 25px clamp(22px, 5vw, 84px); scroll-behavior: smooth; }
  &__welcome {
    max-width: 920px; margin: 8px auto; padding: clamp(18px, 3vw, 32px);
    border: 1px solid $line; border-radius: $panel-radius; background: linear-gradient(112deg, $hero-glow, $hero 73%);
    h2 { @include page-title; margin: 12px 0 7px; }
    p { line-height: 1.75; color: $muted; font-size: 14px; }
  }
  &__prompts {
    display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px;
    button {
      min-height: 34px; background: $surface; border: 1px solid $line; border-radius: $control-radius;
      padding: 7px 10px; color: $green; font-size: 12px; text-align: left;
      &:enabled:hover { border-color: $green; }
    }
  }
  &__symbol {
    display: grid; place-items: center; width: 38px; height: 38px;
    color: $on-green; background: $green; border-radius: 8px; font-size: 22px;
  }
  &__message {
    max-width: 920px; margin: 0 auto 10px; padding: 20px 0; border-bottom: 1px solid $line;
    &--user .chat__body { background: $selection; padding: 12px 15px; border-radius: $panel-radius; }
  }
  &__files {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(min(235px, 100%), 1fr)); gap: 7px; margin-top: 8px;
    &--draft { margin: 0 0 8px; max-height: 125px; overflow-y: auto; }
  }
  &__material-link { padding: 8px; color: $green; font-size: 12px; }
  &__speaker { font-size: 12px; color: $green; margin-bottom: 13px; display: flex; align-items: center; gap: 16px; span { font-size: 10px; color: $muted; } }
  &__body { overflow-wrap: anywhere; font-size: 14px; line-height: 1.95; }
  &__composer {
    width: 100%; max-width: calc(920px + clamp(44px, 10vw, 168px)); margin-inline: auto;
    padding: 14px clamp(22px, 5vw, 84px) 16px; background: $paper;
  }
  &__disclaimer { text-align: center; color: $muted; font-size: 11px; margin-top: 8px; }
  &__error { color: $log-error; background: $log-error-bg; padding: 10px 12px; border-radius: $control-radius; font-size: 12px; margin-bottom: 10px; overflow-wrap: anywhere; }
  &__run { color: $log-error; font-size: 12px; margin-bottom: 8px; overflow-wrap: anywhere; }
  &__connection, &__hint { font-size: 12px; color: $muted; line-height: 1.9; }
  &__link { background: none; border: 0; color: $green; text-decoration: underline; padding: 5px; font-size: 12px; }
}
@media (max-width: 1100px) { .chat { grid-template-columns: 168px minmax(0, 1fr); } }
@media (max-width: 760px) {
  .chat {
    grid-template-columns: minmax(0, 1fr); height: auto; min-height: calc(100dvh - 170px);
    &__history {
      max-height: 190px; border-right: 0; border-bottom: 1px solid $line; gap: 8px; padding: 12px 14px;
      nav { display: flex; overflow-x: auto; }
    }
    &__conversation { max-width: 180px; flex-shrink: 0; }
    &__history-heading, &__local { display: none; }
    &__hint br { display: none; }
    &__transcript { min-height: 250px; max-height: 60dvh; padding-inline: 14px; }
    &__welcome { margin-block: 0; }
    &__composer { padding-inline: 14px; }
  }
}
@media (max-width: 540px) { .chat { &__top { gap: 10px; } &__top-actions { max-width: 55%; flex: 0 0 auto; flex-direction: column; align-items: flex-end; } } }
@media (prefers-reduced-motion: reduce) { .chat__transcript { scroll-behavior: auto; } }
</style>
