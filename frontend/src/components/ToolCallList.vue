<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { authoringAction, createdObjectLink } from './chat/authoringTools'
import ExecutionProgress from './chat/ExecutionProgress.vue'
import { getToolCalls, type ToolCall } from '../api/tools'
import { copyDiagnosticId } from '../utils/diagnosticClipboard'
import type { SourceChunk } from './chat/ChunkSourceCard.vue'

const props = defineProps<{
  runId: string
  messageStatus: string
  preload?: boolean
  summary?: string
  progressTarget?: HTMLElement | null
}>()
const emit = defineEmits<{ 'auto-chunks': [chunks: SourceChunk[]] }>()
const calls = ref<ToolCall[]>([])
const opened = ref(props.messageStatus === 'streaming')
const loading = ref(false)
const error = ref('')
const copyFeedback = ref<{ id: string; message: string } | null>(null)
const POLL_MS = 1000
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
let alive = true
const statuses: Record<ToolCall['status'], string> = {
  awaiting_approval: '等待审批', running: '正在执行', completed: '执行成功', failed: '执行失败',
  cancelled: '已停止', denied: '已拒绝', expired: '已过期',
}
const errors: Record<string, string> = {
  TOOL_TIMEOUT: '查询超时', TOOL_INVALID_ARGUMENTS: '查询条件无效', TOOL_CALL_LIMIT: '本次回答的查询次数已达上限',
  TOOL_EXECUTION_FAILED: '查询服务暂时不可用', PROCESS_RESTARTED: '服务重启中断了查询',
  WORKSPACE_ACCESS_DENIED: '空间权限已失效', WORKSPACE_ARCHIVED: '空间已归档', ACCESS_REVOKED: '执行权限已失效',
  KNOWLEDGE_ACCESS_DENIED: '没有可访问的知识库', KNOWLEDGE_DISABLED: '知识库未启用',
  KNOWLEDGE_RETRIEVAL_TIMEOUT: '知识库检索超时', KNOWLEDGE_RETRIEVAL_FAILED: '知识库检索失败',
  RAGFLOW_NOT_CONFIGURED: '知识库服务未配置', RAGFLOW_UNAVAILABLE: '知识库服务不可用',
  RAGFLOW_TIMEOUT: '知识库服务超时', RAGFLOW_REJECTED: '知识库服务拒绝了操作',
  PERSONAL_CONNECTOR_UNBOUND: '请在当前空间的连接器页面绑定我的账号',
  PERSONAL_CREDENTIAL_INVALID: '请在连接器页面重新绑定我的账号',
  CONNECTOR_KEY_UNAVAILABLE: '请联系管理员配置个人凭证加密密钥',
  CONNECTOR_NOT_LOGGED_IN: '账号授权无效，请在连接器页面检查凭证',
  CONNECTOR_NOT_CONFIGURED: '连接器缺少运行依赖，请联系管理员',
  CONNECTOR_RECORD_NOT_FOUND: '来源已失效，请重新搜索',
  APPROVAL_REJECTED: '用户拒绝了该操作', APPROVAL_EXPIRED: '审批已过期', APPROVAL_CANCELLED: '审批已取消',
  APPROVAL_UNAVAILABLE: '当前环境未启用写操作审批', SCHEDULE_DISABLED: '定时任务未启用',
}
function statusText(call: ToolCall): string {
  if (call.status === 'completed') return call.approval_required ? '执行成功' : '查询成功'
  return statuses[call.status]
}
async function copyId(id: string, event: MouseEvent): Promise<void> {
  const field = (event.currentTarget as HTMLElement).parentElement?.querySelector('code') ?? null
  const runId = props.runId
  const message = await copyDiagnosticId(id, field)
  if (alive && props.runId === runId) copyFeedback.value = { id, message }
}
function resultText(call: ToolCall): string {
  if (!call.result) return ''
  try {
    if (call.tool_name === 'search_knowledge_base' || call.tool_name === 'auto_knowledge_retrieval') {
      const data = JSON.parse(call.result) as { chunks?: { document_name?: unknown }[] }
      const chunks = Array.isArray(data.chunks) ? data.chunks : []
      if (!chunks.length) return '知识库检索完成，未命中可引用来源。'
      const names = [...new Set(chunks.map(chunk => typeof chunk.document_name === 'string' ? chunk.document_name : '未命名文档'))].slice(0, 3)
      return '知识库检索完成，命中 ' + chunks.length + ' 条来源：' + names.join('、')
    }
    if (call.tool_name === 'list_knowledge_bases') {
      const data = JSON.parse(call.result) as { count?: unknown; names?: unknown }
      const names = Array.isArray(data.names) ? data.names.filter((item): item is string => typeof item === 'string') : []
      return '知识库目录：' + (typeof data.count === 'number' ? data.count : names.length) + ' 个' + (names.length ? '（' + names.slice(0, 3).join('、') + '）' : '')
    }
    if (call.tool_name === 'create_scheduled_task') {
      const data = JSON.parse(call.result) as { name?: unknown; cron?: unknown; cron_expression?: unknown; schedule_deleted?: boolean }
      if (data.schedule_deleted) return '已创建的定时任务后来已删除'
      return '已创建定时任务：' + (typeof data.name === 'string' ? data.name : '未命名') + (typeof (data.cron ?? data.cron_expression) === 'string' ? ' · ' + (data.cron ?? data.cron_expression) : '')
    }
    if (authoringAction(call.tool_name)) return '执行成功'
    if (call.tool_name === 'load_skill') {
      const data = JSON.parse(call.result) as { key?: unknown; version?: unknown }
      return '已加载技能：' + (typeof data.key === 'string' ? data.key : '未知') + (typeof data.version === 'number' ? ' · v' + data.version : '')
    }
    const data = JSON.parse(call.result) as { days?: unknown; total?: unknown; counts?: Record<string, unknown> }
    if (typeof data.days !== 'number' || typeof data.total !== 'number' || !data.counts) return '查询已完成'
    const labels: Record<string, string> = { pending: '等待生成', running: '正在生成', completed: '已完成', failed: '失败', cancelled: '已停止' }
    const parts = Object.entries(labels).filter(([key]) => typeof data.counts?.[key] === 'number' && Number(data.counts[key]) > 0)
      .map(([key, label]) => label + ' ' + String(data.counts?.[key]))
    return '最近 ' + data.days + ' 天，共 ' + data.total + ' 次运行（包含本次）。' + parts.join(' · ')
  } catch { return '查询结果暂时无法展示' }
}
interface Progress { title?: string; role?: string; output?: string; steps?: { title: string; status: string }[]; attempt?: number; max_attempts?: number; next_retry_at?: string }
function data(call: ToolCall): Progress {
  try { return call.result ? (JSON.parse(call.result) as Progress | null) ?? {} : {} } catch { return {} }
}
const steps = computed(() => {
  const plan = [...calls.value].reverse().find(call => call.tool_name === 'update_plan' && call.status === 'completed')
  const items = plan ? data(plan).steps : undefined
  return Array.isArray(items) ? items.filter(step => typeof step?.title === 'string' && typeof step.status === 'string') : []
})
const agents = computed(() => calls.value.filter(call => call.tool_name === 'delegate_agent').map(call => {
  const { title, role, output } = data(call)
  return {
    id: call.id, status: call.status, title, role, output,
    error: call.error_code && (errors[call.error_code] ?? '子任务未完成，请参考主 Agent 的说明'),
  }
}))
const tools = computed(() => calls.value.filter(call => !['update_plan', 'delegate_tasks', 'delegate_agent', 'model_request'].includes(call.tool_name)))
const objectLinks = computed(() => calls.value.flatMap(call => {
  const link = createdObjectLink(call)
  return link ? [{ ...link, callId: call.id }] : []
}))
function toolLabel(call: ToolCall): string {
  const names: Record<string, string> = {
    get_my_run_stats: '我的运行统计', search_knowledge_base: '知识库检索',
    inspect_analysis_data: '检查报表数据', run_python_analysis: '运行 Python 生成报表',
    auto_knowledge_retrieval: '自动检索知识库', list_knowledge_bases: '知识库目录',
    load_skill: '加载技能',
  }
  return authoringAction(call.tool_name) ?? names[call.tool_name] ?? '工具调用'
}
const activeStage = computed(() => {
  if (props.messageStatus !== 'streaming') return ''
  const call = [...calls.value].reverse().find(item => item.status === 'running' || item.status === 'awaiting_approval')
  if (!call) return ''
  if (call.status === 'awaiting_approval') return '等待审批'
  if (call.tool_name === 'delegate_agent') return '子 Agent 协作中'
  if (call.tool_name === 'model_request') return data(call).next_retry_at ? '等待重试' : '正在生成'
  return toolLabel(call) + '中'
})
const retries = computed(() => calls.value.filter(call => call.tool_name === 'model_request' && (call.status === 'failed' || (call.status === 'running' && data(call).next_retry_at))))
const autoRetrieve = computed(() => {
  const call = [...calls.value].reverse().find(c => c.tool_name === 'auto_knowledge_retrieval' && c.status === 'completed' && c.result)
  if (!call?.result) return []
  try {
    const parsed = JSON.parse(call.result) as { chunks?: unknown }
    const unique = new Map<number, SourceChunk>()
    if (Array.isArray(parsed?.chunks)) {
      for (const chunk of parsed.chunks) {
        if (chunk && Number.isSafeInteger(chunk.index) && chunk.index > 0
          && typeof chunk.content === 'string' && chunk.content.trim() && typeof chunk.document_name === 'string'
          && !unique.has(chunk.index)) {
          unique.set(chunk.index, {
            index: chunk.index, content: chunk.content, document_name: chunk.document_name || '未命名文档',
            similarity: typeof chunk.similarity === 'number' ? chunk.similarity : null,
          })
        }
      }
    }
    return [...unique.values()]
  } catch { return [] }
})
watch(autoRetrieve, chunks => emit('auto-chunks', chunks), { immediate: true })
const now = ref(Date.now())
const clock = setInterval(() => { now.value = Date.now() }, 1000)
function remaining(call: ToolCall): number {
  return Math.max(0, Math.ceil((Date.parse(data(call).next_retry_at ?? '') - now.value) / 1000)) || 0
}
async function load(): Promise<void> {
  clearTimeout(timer)
  const token = ++generation
  loading.value = true
  error.value = ''
  try {
    const rows = await getToolCalls(props.runId)
    if (!alive || token !== generation) return
    calls.value = rows
  } catch (value) {
    if (alive && token === generation) error.value = value instanceof Error ? value.message : '暂时无法读取工具记录'
  } finally {
    if (alive && token === generation) {
      loading.value = false
      // 当前轮的进度入口位于标题栏，收起消息里的工具记录也要继续更新。
      if ((opened.value || props.progressTarget) && props.messageStatus === 'streaming' && !error.value) {
        timer = setTimeout(() => { void load() }, POLL_MS)
      }
    }
  }
}
function toggle(event: Event): void {
  if (event.target !== event.currentTarget) return
  const open = (event.target as HTMLDetailsElement).open
  if (open === opened.value) return
  opened.value = open
  if (opened.value) void load()
  else if (!props.preload && !props.progressTarget) { generation++; clearTimeout(timer); loading.value = false }
}
onMounted(() => { if (opened.value || props.preload || props.progressTarget) void load() })
watch(() => [props.messageStatus, props.preload, props.progressTarget] as const, ([status], [previous]) => {
  if (previous === 'streaming' && status !== 'streaming') opened.value = false
  if (opened.value || props.preload || props.progressTarget || previous === 'streaming') void load()
})
onBeforeUnmount(() => { alive = false; generation++; clearTimeout(timer); clearInterval(clock) })
</script>

<template>
  <details
    class="tool-calls"
    :open="opened"
    @toggle="toggle"
  >
    <summary class="tool-calls__heading">
      {{ activeStage || summary || (messageStatus === 'streaming' ? '正在处理' : '执行记录') }}
      <span>· {{ opened ? '收起执行过程' : '查看执行过程' }}</span>
    </summary>
    <p
      v-if="error"
      class="tool-calls__error"
      role="alert"
    >
      {{ error }} <button
        type="button"
        @click="load"
      >
        重新读取
      </button>
    </p>
    <p
      v-else-if="loading && !calls.length"
      class="tool-calls__hint"
    >
      正在读取执行记录…
    </p>
    <p
      v-else-if="!calls.length"
      class="tool-calls__hint"
    >
      本次回答尚无工具调用记录。
    </p>
    <Teleport
      :to="progressTarget ?? 'body'"
      :disabled="!progressTarget"
    >
      <ExecutionProgress
        :in-header="Boolean(progressTarget)"
        :streaming="messageStatus === 'streaming'"
        :steps="steps"
        :agents="agents"
        :error="error"
        :loading="loading"
        @retry="load"
      />
    </Teleport>
    <p
      v-for="retry in retries"
      :key="retry.id"
      class="tool-calls__hint"
      role="status"
    >
      <template v-if="retry.status === 'running'">
        网络异常，{{ remaining(retry) }} 秒后重试 · 已请求 {{ data(retry).attempt }}/{{ data(retry).max_attempts }} 次
      </template>
      <template v-else>
        模型请求失败 · {{ retry.error_code }}，可在当前对话重试。
      </template>
    </p>
    <ol
      v-if="tools.length"
      class="tool-calls__list"
    >
      <li
        v-for="call in tools"
        :key="call.id"
        class="tool-calls__item"
      >
        <div class="tool-calls__line">
          <span>{{ toolLabel(call) }}<em v-if="call.approval_required"> · 需审批</em></span>
          <span :class="{ 'tool-calls__status--failed': call.status === 'failed' || call.status === 'denied' || call.status === 'expired' }">{{ statusText(call) }}</span>
        </div>
        <p
          v-if="call.status === 'completed'"
          class="tool-calls__result"
        >
          {{ resultText(call) }}
        </p>
        <p
          v-else-if="call.error_code && call.status !== 'cancelled'"
          class="tool-calls__hint"
        >
          {{ errors[call.error_code] ?? '查询未完成' }}
        </p>
        <div
          v-if="call.status === 'failed'"
          class="tool-calls__diagnostic"
        >
          <span>诊断编号 <code>{{ call.id }}</code></span>
          <button
            type="button"
            @click="copyId(call.id, $event)"
          >
            复制诊断编号
          </button>
          <span
            v-if="copyFeedback?.id === call.id"
            role="status"
          >{{ copyFeedback.message }}</span>
        </div>
      </li>
    </ol>
  </details>
  <div
    v-if="objectLinks.length"
    class="tool-calls__objects"
    aria-label="操作结果"
  >
    <RouterLink
      v-for="link in objectLinks"
      :key="link.callId"
      :to="link.to"
    >
      {{ link.label }}
    </RouterLink>
  </div>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.tool-calls {
  margin: 8px 0 16px; font-size: 12px;
  &[open] { padding: 8px 12px; border-left: 2px solid $line; background: $sidebar; }
  &__objects { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 8px 0 12px; font-size: 13px; a { color: $green; } }
  &__heading { color: $muted; cursor: pointer; padding: 3px 0; span { color: $green; } }
  &__list { padding: 0; margin: 12px 0 0; list-style: none; }
  &__item + &__item { margin-top: 14px; padding-top: 12px; border-top: 1px solid $line; }
  &__line { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; color: $green; em { color: $log-warning; font-style: normal; font-size: 10px; } }
  &__status--failed, &__error { color: $log-error; }
  &__result, &__hint, &__error { line-height: 1.9; margin: 8px 0 0; overflow-wrap: anywhere; }
  &__hint { color: $muted; }
  &__diagnostic {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; margin-top: 6px;
    color: $muted; font-size: 11px; overflow-wrap: anywhere;
    button { padding: 0; }
  }
  button { color: $green; background: transparent; border: 0; text-decoration: underline; }
}
</style>
