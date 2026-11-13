<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { getToolCalls, type ToolCall } from '../api/tools'

const props = defineProps<{ runId: string; messageStatus: string }>()
const emit = defineEmits<{ 'auto-chunks': [chunks: Array<{ index: number; content: string; document_name: string; similarity: number | null }>] }>()
const calls = ref<ToolCall[]>([])
const opened = ref(props.messageStatus === 'streaming')
const loading = ref(false)
const error = ref('')
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
  APPROVAL_REJECTED: '用户拒绝了该操作', APPROVAL_EXPIRED: '审批已过期', APPROVAL_CANCELLED: '审批已取消',
  APPROVAL_UNAVAILABLE: '当前环境未启用写操作审批', SCHEDULE_DISABLED: '定时任务未启用',
}
function statusText(call: ToolCall): string {
  if (call.status === 'completed') return call.approval_required ? '执行成功' : '查询成功'
  return statuses[call.status]
}
function resultText(call: ToolCall): string {
  if (!call.result) return ''
  try {
    if (call.tool_name === 'search_knowledge_base') {
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
      const data = JSON.parse(call.result) as { name?: unknown; cron_expression?: unknown }
      return '已创建定时任务：' + (typeof data.name === 'string' ? data.name : '未命名') + (typeof data.cron_expression === 'string' ? ' · ' + data.cron_expression : '')
    }
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
  try { return call.result ? JSON.parse(call.result) as Progress : {} } catch { return {} }
}
const plan = computed(() => [...calls.value].reverse().find(call => call.tool_name === 'update_plan' && call.status === 'completed'))
const agents = computed(() => calls.value.filter(call => call.tool_name === 'delegate_agent'))
const tools = computed(() => calls.value.filter(call => !['update_plan', 'delegate_tasks', 'delegate_agent', 'model_request', 'auto_knowledge_retrieval'].includes(call.tool_name)))
const retries = computed(() => calls.value.filter(call => call.tool_name === 'model_request' && (call.status === 'failed' || (call.status === 'running' && data(call).next_retry_at))))
const autoRetrieve = computed(() => {
  const call = [...calls.value].reverse().find(c => c.tool_name === 'auto_knowledge_retrieval' && c.status === 'completed' && c.result)
  if (!call?.result) return []
  try {
    const parsed = JSON.parse(call.result) as { chunks?: Array<{ index: number; content: string; document_name: string; similarity: number | null }> }
    return Array.isArray(parsed.chunks) ? parsed.chunks : []
  } catch { return [] }
})
watch(autoRetrieve, chunks => { if (chunks.length) emit('auto-chunks', chunks) }, { immediate: true })
const stepLabels: Record<string, string> = { pending: '待处理', in_progress: '进行中', completed: '已完成' }
const agentLabels: Record<string, string> = { awaiting_approval: '等待审批', running: '执行中', completed: '已完成', failed: '失败', cancelled: '已停止', denied: '已拒绝', expired: '已过期' }
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
      if (opened.value && props.messageStatus === 'streaming' && !error.value) timer = setTimeout(() => { void load() }, POLL_MS)
    }
  }
}
function toggle(event: Event): void {
  opened.value = (event.target as HTMLDetailsElement).open
  if (opened.value) void load()
  else { generation++; clearTimeout(timer); loading.value = false }
}
onMounted(() => { if (opened.value) void load() })
watch(() => props.messageStatus, () => { if (opened.value) void load() })
onBeforeUnmount(() => { alive = false; generation++; clearTimeout(timer); clearInterval(clock) })
</script>

<template>
  <details
    class="tool-calls"
    :open="opened"
    @toggle="toggle"
  >
    <summary class="tool-calls__heading">
      规划与执行 · 工具调用记录
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
    <section
      v-if="plan"
      class="tool-calls__plan"
      aria-label="工作计划"
    >
      <h3>工作计划</h3>
      <p class="tool-calls__hint">
        主 Agent 的工作步骤；实际执行状态见下方记录。
      </p>
      <ol>
        <li
          v-for="(step, index) in data(plan).steps"
          :key="index"
        >
          {{ step.title }} · {{ stepLabels[step.status] ?? step.status }}
        </li>
      </ol>
    </section>
    <section
      v-if="agents.length"
      aria-label="子 Agent 协作"
    >
      <h3>子 Agent 协作</h3>
      <article
        v-for="agent in agents"
        :key="agent.id"
        class="tool-calls__item"
      >
        <div class="tool-calls__line">
          <span>{{ data(agent).title ?? '子任务' }} · {{ data(agent).role === 'reviewer' ? '复核员' : '分析员' }}</span>
          <span>{{ agentLabels[agent.status] }}</span>
        </div>
        <details v-if="agent.status === 'completed' && data(agent).output">
          <summary>查看结果</summary><p class="tool-calls__output">
            {{ data(agent).output }}
          </p>
        </details>
        <p
          v-if="agent.error_code"
          class="tool-calls__error"
        >
          {{ errors[agent.error_code] ?? '子任务未完成，请参考主 Agent 的说明' }}
        </p>
      </article>
    </section>
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
          <span>{{ call.tool_name === 'get_my_run_stats' ? '我的运行统计' : call.tool_name === 'search_knowledge_base' ? '知识库检索' : call.tool_name === 'list_knowledge_bases' ? '知识库目录' : call.tool_name === 'create_scheduled_task' ? '创建定时任务' : call.tool_name === 'load_skill' ? '加载技能' : '工具查询' }}<em v-if="call.approval_required"> · 需审批</em></span>
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
      </li>
    </ol>
  </details>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.tool-calls {
  margin-top: 16px; padding: 10px 14px; border-left: 2px solid $line; background: $paper; font-size: 12px;
  &__heading { color: $muted; cursor: pointer; padding: 3px 0; }
  h3 { font-size: 12px; color: $green; margin: 12px 0; }
  &__plan li { margin: 8px 0; }
  &__output { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.9; }
  &__list { padding: 0; margin: 12px 0 0; list-style: none; }
  &__item + &__item { margin-top: 14px; padding-top: 12px; border-top: 1px solid $line; }
  &__line { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; color: $green; em { color: #9a6a35; font-style: normal; font-size: 10px; } }
  &__status--failed, &__error { color: #934b40; }
  &__result, &__hint, &__error { line-height: 1.9; margin: 8px 0 0; overflow-wrap: anywhere; }
  &__hint { color: $muted; }
  button { color: $green; background: transparent; border: 0; text-decoration: underline; }
}
</style>
