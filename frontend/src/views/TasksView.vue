<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { confirmTask, controlTask, createTask, getTask, isTerminal, listTasks, updateTaskPlan, watchTask, type TaskAction, type TaskControl, type TaskSnapshot, type TaskStatus } from '../api/tasks'

const route = useRoute()
const router = useRouter()
const tasks = ref<TaskSnapshot[]>([])
const snapshot = ref<TaskSnapshot | null>(null)
const goal = ref('')
const loading = ref(false)
const submitting = ref(false)
const operation = ref('')
const error = ref('')
const connection = ref('')
const editingPlan = ref(false)
const planText = ref('')
const more = ref(false)
const now = ref(Date.now())
const currentId = computed(() => typeof route.params.taskId === 'string' ? route.params.taskId : '')
const waitingActions = computed(() => ['running', 'retry_wait', 'paused', 'planning'].includes(snapshot.value?.run.status ?? '') ? snapshot.value?.actions.filter(action => action.status === 'retry_wait' || action.next_retry_at !== null) ?? [] : [])
const status = computed(() => snapshot.value?.run.status)
const completedSteps = computed(() => snapshot.value?.steps.filter(step => step.status === 'succeeded').length ?? 0)
const canPause = computed(() => status.value === 'running' || status.value === 'retry_wait')
const canCancel = computed(() => !!status.value && !isTerminal(status.value))
const statusLabels: Record<TaskStatus, string> = { pending: '等待规划', planning: '正在规划', awaiting_confirmation: '等待确认计划', running: '正在执行', retry_wait: '等待重试', paused: '已暂停', completed: '已完成', failed: '执行失败', cancelled: '已停止' }
const stepLabels: Record<string, string> = { pending: '等待执行', running: '正在执行', retry_wait: '等待重试', paused: '已暂停', succeeded: '已完成', failed: '执行失败', cancelled: '已停止', skipped: '已跳过' }
const kindLabels: Record<string, string> = { analysis: '分析', tool: '工具查询', synthesis: '汇总' }
const errorLabels: Record<string, string> = {
  ACCESS_REVOKED: '任务权限已失效，执行已停止。', DEADLINE_EXCEEDED: '任务超过总执行时限。', MODEL_NOT_CONFIGURED: '尚未配置模型服务。',
  TASK_RETRY_EXHAUSTED: '已达到重试次数上限。', NETWORK_TIMEOUT: '请求连接或读取超时。', NETWORK_UNAVAILABLE: '连接暂时不可用。', RATE_LIMITED: '服务暂时限流，将按服务要求等待。', MODEL_AUTH_DENIED: '模型服务拒绝认证，请检查配置。', MODEL_ACCOUNT_ERROR: '模型账号或额度异常，请检查服务配置。', HTTP_503: '模型服务暂时不可用。', HTTP_502: '上游服务暂时不可用。', HTTP_504: '上游服务响应超时。', HTTP_500: '上游服务暂时出错。',
  RETRY_EXHAUSTED: '已达到重试次数上限。', MODEL_CALL_FAILED: '模型服务调用失败。', INVALID_PLAN: '计划未通过校验，请重新规划。',
  TOOL_NOT_ALLOWED: '计划中的工具未获授权。', OUTCOME_UNKNOWN: '请求结果尚不确定，需要核对后再继续。', PROCESS_RESTARTED: '服务重启中断了本次尝试。',
}
const POLL_MS = 1000
const RECONNECT_MS = 3000
const CLOCK_MS = 500
let timer: ReturnType<typeof setTimeout> | undefined
let clock: ReturnType<typeof setInterval> | undefined
let revision = 0
let listRevision = 0
let readSequence = 0
let alive = true
let streamConnected = false
let disposeSource: (() => void) | null = null
let pending: { goal: string; key: string } | null = null

function errorText(code: string | null): string { return code ? errorLabels[code] ?? '本次执行未完成（' + code + '）。' : '' }
function describeError(value: unknown): string {
  const message = value instanceof Error ? value.message : '操作失败，请重试'
  return value instanceof ApiError && value.requestId ? message + ' · 请求标识：' + value.requestId : message
}
function actionName(action: TaskAction): string {
  if (action.kind === 'plan') return '生成计划'
  const title = snapshot.value?.plan?.steps.find(step => step.key === action.step_key)?.title
  return (title ? title + ' · ' : '') + (action.kind === 'tool' ? '工具查询' : '模型请求')
}
function actionStatus(action: TaskAction): string {
  if (['running', 'pending', 'retry_wait'].includes(action.status)) {
    if (status.value === 'cancelled') return '随任务停止'
    if (status.value === 'paused') return '随任务暂停'
    if (status.value === 'failed') return '随任务中断'
  }
  const labels: Record<string, string> = { pending: '等待请求', running: '正在请求', retry_wait: '等待重试', succeeded: '已完成', failed: '请求失败', interrupted: '已中断', cancelled: '已停止', paused: '已暂停' }
  return labels[action.status] ?? action.status
}
function countdown(value: string | null | undefined): string {
  if (!value) return '正在安排下一次尝试'
  const at = Date.parse(value)
  if (!Number.isFinite(at)) return '正在安排下一次尝试'
  const seconds = Math.max(0, Math.ceil((at - now.value) / 1000))
  return seconds > 0 ? seconds + ' 秒后重试' : '已到重试时间，等待执行'
}
function accept(value: TaskSnapshot): boolean {
  if (!alive || value.task.id !== currentId.value) return false
  const previous = snapshot.value
  if (previous && previous.last_event_sequence > value.last_event_sequence) return false
  if (previous?.run.id === value.run.id && previous.run.version > value.run.version) return false
  snapshot.value = value
  if (value.run.status !== 'awaiting_confirmation') editingPlan.value = false
  return true
}
async function refreshList(append = false): Promise<void> {
  const token = ++listRevision
  try {
    const rows = await listTasks(append ? tasks.value.length : 0)
    if (!alive || token !== listRevision) return
    tasks.value = append ? [...tasks.value, ...rows] : rows
    more.value = rows.length === 20
  } catch (value) { if (alive && token === listRevision) connection.value = describeError(value) }
}
function schedule(token: number, delay = POLL_MS): void {
  clearTimeout(timer)
  if (!streamConnected && alive && token === revision && currentId.value && !isTerminal(snapshot.value?.run.status ?? 'pending')) {
    timer = setTimeout(() => { void read(token, false) }, delay)
  }
}
function subscribe(token: number): void {
  if (disposeSource || !snapshot.value || isTerminal(snapshot.value.run.status)) return
  const id = currentId.value
  disposeSource = watchTask(id, snapshot.value.last_event_sequence, value => {
    if (!alive || token !== revision || id !== currentId.value) return
    const previousStatus = snapshot.value?.run.status
    if (!accept(value)) return
    streamConnected = true; clearTimeout(timer); connection.value = ''
    if (isTerminal(value.run.status) && previousStatus !== value.run.status) void refreshList()
  }, message => {
    if (!alive || token !== revision || id !== currentId.value) return
    streamConnected = !message; connection.value = message
    if (message) schedule(token, RECONNECT_MS)
  })
}
async function read(token: number, initial: boolean): Promise<void> {
  const id = currentId.value
  if (!id) return
  const sequence = ++readSequence
  let retry = true
  let delay = POLL_MS
  try {
    const value = await getTask(id)
    if (!alive || token !== revision || sequence !== readSequence || id !== currentId.value) return
    const previousStatus = snapshot.value?.run.status
    accept(value); connection.value = ''; subscribe(token)
    if (initial || !snapshot.value) error.value = ''
    if (isTerminal(value.run.status) && previousStatus !== value.run.status) void refreshList()
  } catch (value) {
    if (!alive || token !== revision || sequence !== readSequence) return
    retry = !(value instanceof ApiError && [400, 401, 403, 404].includes(value.status))
    if (value instanceof ApiError && value.status === 403) snapshot.value = null
    if (initial && !snapshot.value) error.value = describeError(value)
    else connection.value = '暂时无法读取进度，正在重新连接。' + describeError(value)
    delay = RECONNECT_MS
  } finally {
    if (alive && token === revision && sequence === readSequence) { loading.value = false; if (retry) schedule(token, delay) }
  }
}
async function load(): Promise<void> {
  const token = ++revision
  clearTimeout(timer); disposeSource?.(); disposeSource = null; streamConnected = false
  editingPlan.value = false; planText.value = ''
  snapshot.value = null; error.value = ''; connection.value = ''; operation.value = ''
  if (!currentId.value) { loading.value = false; return }
  loading.value = true
  await read(token, true)
}
async function create(): Promise<void> {
  if (submitting.value || !goal.value.trim()) return
  submitting.value = true; error.value = ''; connection.value = ''
  const value = goal.value
  if (!pending || pending.goal !== value) pending = { goal: value, key: crypto.randomUUID() }
  const token = revision
  try {
    const created = await createTask(value, pending.key)
    pending = null
    if (!alive || token !== revision) return
    goal.value = ''
    await router.push('/tasks/' + created.task.id)
    if (currentId.value === created.task.id) accept(created)
    void refreshList()
  } catch (value) { if (alive && token === revision) error.value = describeError(value) }
  finally { if (alive) submitting.value = false }
}
async function act(action: TaskControl | 'confirm'): Promise<void> {
  const before = snapshot.value
  if (!before || operation.value || loading.value) return
  const token = ++revision
  readSequence++; clearTimeout(timer); disposeSource?.(); disposeSource = null; streamConnected = false
  const id = before.task.id
  operation.value = action; error.value = ''; connection.value = ''
  try {
    const value = action === 'confirm' && before.plan
      ? await confirmTask(id, before.plan.revision, before.plan.digest, before.run.id)
      : action !== 'confirm' ? await controlTask(id, action, before.run.id, before.run.version) : null
    if (!value || !alive || token !== revision || id !== currentId.value) return
    accept(value); subscribe(token); schedule(token); void refreshList()
  } catch (value) { if (alive && token === revision) error.value = describeError(value) }
  finally { if (alive && token === revision) { operation.value = ''; schedule(token) } }
}
function editPlan(): void {
  if (!snapshot.value?.plan) return
  planText.value = JSON.stringify({ steps: snapshot.value.plan.steps }, null, 2)
  editingPlan.value = true; error.value = ''
}
async function savePlan(): Promise<void> {
  const before = snapshot.value
  if (!before || operation.value) return
  let definition: unknown
  try { definition = JSON.parse(planText.value) as unknown }
  catch { error.value = '计划 JSON 格式不正确，请检查后重新保存。'; return }
  const token = ++revision
  readSequence++; clearTimeout(timer); disposeSource?.(); disposeSource = null; streamConnected = false
  operation.value = 'save-plan'; error.value = ''
  try {
    if (!before.plan) return
    const value = await updateTaskPlan(before.task.id, definition, before.run.id, before.plan.revision)
    if (!alive || token !== revision || before.task.id !== currentId.value) return
    accept(value); editingPlan.value = false; subscribe(token); void refreshList()
  } catch (value) { if (alive && token === revision) error.value = describeError(value) }
  finally { if (alive && token === revision) { operation.value = ''; schedule(token) } }
}
function newTask(): void { goal.value = ''; pending = null; void router.push('/tasks') }
watch(currentId, load, { immediate: true })
clock = setInterval(() => { now.value = Date.now() }, CLOCK_MS)
void refreshList()
onBeforeUnmount(() => { alive = false; revision++; listRevision++; clearTimeout(timer); clearInterval(clock); disposeSource?.() })
</script>

<template>
  <div class="tasks">
    <aside class="tasks__history">
      <div class="tasks__history-heading">
        <span>计划与执行</span><button
          type="button"
          aria-label="刷新任务列表"
          @click="refreshList()"
        >
          ↻
        </button>
      </div>
      <button
        class="tasks__new"
        type="button"
        :disabled="submitting"
        @click="newTask"
      >
        ＋ 新建任务
      </button>
      <nav aria-label="任务列表">
        <RouterLink
          v-for="item in tasks"
          :key="item.task.id"
          :to="'/tasks/' + item.task.id"
          class="tasks__history-item"
          :class="{ 'tasks__history-item--active': item.task.id === currentId }"
        ><span>{{ item.task.goal }}</span><small>{{ statusLabels[item.run.status] }}</small></RouterLink>
      </nav>
      <p
        v-if="!tasks.length"
        class="tasks__hint"
      >
        需要多个步骤的工作，<br />从一份明确的计划开始。
      </p>
      <button
        v-if="more"
        type="button"
        class="tasks__link"
        @click="refreshList(true)"
      >
        加载更多
      </button>
      <p class="tasks__private">
        私有任务 · 当前工作空间
      </p>
    </aside>
    <main class="tasks__workspace">
      <template v-if="!currentId">
        <div class="tasks__intro">
          <span class="tasks__eyebrow">SEQUENCE / PLAN & EXECUTE</span><h1>先理清步骤，<br />再把工作推进。</h1><p>描述要完成的目标。序列会先生成计划，<br />经你确认后，才开始逐步执行。</p>
        </div>
        <form
          class="tasks__create"
          @submit.prevent="create"
        >
          <label for="task-goal">这次想完成什么？</label><textarea
            id="task-goal"
            v-model="goal"
            rows="5"
            maxlength="10000"
            :disabled="submitting"
            placeholder="例如：分析我最近 7 天的运行情况，并给出改进建议。"
          />
          <div class="tasks__create-footer">
            <span>明确目标即可，无需填写预算。</span><button
              class="tasks__primary"
              type="submit"
              :disabled="submitting || !goal.trim()"
            >
              {{ submitting ? '正在提交…' : '生成计划 →' }}
            </button>
          </div>
        </form>
      </template>
      <p
        v-else-if="loading && !snapshot"
        class="tasks__empty"
        role="status"
      >
        正在读取计划与执行进度…
      </p>
      <template v-if="snapshot">
        <header class="tasks__top">
          <div><span class="tasks__eyebrow">SEQUENCE / TASK</span><h1>{{ snapshot.task.goal }}</h1></div><span
            class="tasks__status"
            :class="'tasks__status--' + snapshot.run.status"
          >{{ statusLabels[snapshot.run.status] }}</span>
        </header>
        <div class="tasks__controls">
          <span>已完成 {{ completedSteps }} / {{ snapshot.steps.length }} 步</span><div>
            <button
              v-if="canPause"
              type="button"
              :disabled="!!operation"
              @click="act('pause')"
            >
              暂停
            </button><button
              v-if="status === 'paused'"
              type="button"
              :disabled="!!operation"
              @click="act('resume')"
            >
              继续执行
            </button><button
              v-if="status === 'failed' || status === 'cancelled'"
              type="button"
              :disabled="!!operation"
              @click="act('retry')"
            >
              新建执行尝试
            </button><button
              v-if="canCancel"
              type="button"
              class="tasks__stop"
              :disabled="!!operation"
              @click="act('cancel')"
            >
              {{ operation === 'cancel' ? '正在停止…' : '停止任务' }}
            </button>
          </div>
        </div>
        <section
          v-if="status === 'retry_wait' || waitingActions.length || snapshot.steps.some(step => step.status === 'retry_wait')"
          class="tasks__waiting"
          role="status"
        >
          <span class="tasks__waiting-mark">↻</span><div>
            <strong>{{ status === 'paused' ? '任务已暂停，恢复后仍需遵守服务要求的等待时间。' : '请求暂时未完成，' + countdown(snapshot.run.next_retry_at ?? snapshot.steps.find(step => step.status === 'retry_wait')?.next_retry_at) }}</strong><p>{{ status === 'paused' ? '已经完成的步骤会保留。你可以恢复或停止任务。' : '已经完成的步骤会保留。你可以等待、暂停或停止任务。' }}</p>
            <p
              v-for="action in waitingActions"
              :key="action.id"
              class="tasks__retry-attempt"
            >
              {{ actionName(action) }}：已尝试 {{ action.attempt_count }}/{{ action.max_attempts }} 次 · {{ countdown(action.next_retry_at) }}<span v-if="action.error_code"> · {{ errorText(action.error_code) }}</span>
            </p>
          </div>
        </section>
        <p
          v-if="snapshot.run.error_code"
          class="tasks__run-error"
          role="status"
        >
          {{ errorText(snapshot.run.error_code) }}
        </p>
        <section
          v-if="snapshot.plan"
          class="tasks__plan"
          aria-label="执行计划"
        >
          <div class="tasks__section-heading">
            <h2>执行计划 <small>版本 {{ snapshot.plan.revision }}</small></h2><span>{{ snapshot.plan.steps.length }} 个步骤</span>
          </div>
          <p
            v-if="status === 'awaiting_confirmation'"
            class="tasks__confirm-hint"
          >
            请检查以下步骤。确认前不会执行工具；确认后按这份计划推进。
          </p>
          <ol class="tasks__steps">
            <li
              v-for="(step, index) in snapshot.plan.steps"
              :key="step.key"
              class="tasks__step"
            >
              <span class="tasks__number">{{ String(index + 1).padStart(2, '0') }}</span><div class="tasks__step-main">
                <div class="tasks__step-title">
                  <h3>{{ step.title }}</h3><span>{{ kindLabels[step.kind] }}</span>
                </div><p class="tasks__instruction">
                  {{ step.instruction }}
                </p><p
                  v-if="step.depends_on.length"
                  class="tasks__dependency"
                >
                  依赖：{{ step.depends_on.map(key => snapshot?.plan?.steps.find(item => item.key === key)?.title ?? key).join('、') }}
                </p><p
                  v-if="step.tool_name"
                  class="tasks__tool"
                >
                  {{ step.tool_name === 'get_my_run_stats' ? '本人当前空间的运行统计' : step.tool_name }}<span v-if="typeof step.arguments.days === 'number'"> · 最近 {{ step.arguments.days }} 天</span>
                </p><template
                  v-for="result in snapshot.steps.filter(item => item.key === step.key)"
                  :key="result.key"
                >
                  <div class="tasks__step-progress">
                    <span :class="'tasks__step-state--' + result.status">{{ stepLabels[result.status] }}</span><small v-if="result.attempt_count">步骤启动 {{ result.attempt_count }} 次</small><small v-if="result.status === 'retry_wait'">{{ countdown(result.next_retry_at) }}</small>
                  </div><p
                    v-if="result.error_code"
                    class="tasks__step-error"
                  >
                    {{ errorText(result.error_code) }}
                  </p><details
                    v-if="result.result"
                    class="tasks__step-result"
                  >
                    <summary>查看步骤结果</summary><div>{{ result.result }}</div>
                  </details>
                </template>
              </div>
            </li>
          </ol>
          <form
            v-if="editingPlan && status === 'awaiting_confirmation'"
            class="tasks__editor"
            @submit.prevent="savePlan"
          >
            <label for="plan-json">修改计划草案</label>
            <p>可编辑步骤说明、依赖及查询天数；保存后会生成新版本，仍需确认才执行。</p>
            <textarea
              id="plan-json"
              v-model="planText"
              rows="14"
              spellcheck="false"
              :disabled="!!operation"
            />
            <div>
              <button
                class="tasks__primary"
                type="submit"
                :disabled="!!operation"
              >
                {{ operation === 'save-plan' ? '正在保存…' : '保存新版本' }}
              </button><button
                type="button"
                :disabled="!!operation"
                @click="editingPlan = false"
              >
                放弃修改
              </button>
            </div>
          </form>
          <div
            v-if="status === 'awaiting_confirmation' && !editingPlan"
            class="tasks__confirm"
          >
            <p>只有你确认后，才会开始执行。</p><button
              type="button"
              class="tasks__edit"
              :disabled="!!operation"
              @click="editPlan"
            >
              修改计划
            </button><button
              class="tasks__primary"
              type="button"
              :disabled="!!operation"
              @click="act('confirm')"
            >
              {{ operation === 'confirm' ? '正在确认…' : '确认计划并执行' }}
            </button>
          </div>
        </section>
        <section
          v-else
          class="tasks__empty"
          role="status"
        >
          {{ status === 'pending' || status === 'planning' ? '正在整理可执行的步骤，请稍候…' : '本次尚未生成有效计划。' }}
        </section>
        <details
          v-if="snapshot.actions.length"
          class="tasks__actions"
        >
          <summary>请求尝试记录 <span>{{ snapshot.actions.length }} 个逻辑请求</span></summary>
          <p class="tasks__actions-hint">
            每行对应一次逻辑模型或工具请求；网络重试次数由后端记录，与步骤启动次数分开。
          </p>
          <ol>
            <li
              v-for="action in snapshot.actions"
              :key="action.id"
            >
              <div><strong>{{ actionName(action) }}</strong><span>{{ actionStatus(action) }}</span></div>
              <p>
                {{ action.attempt_count ? '已尝试 ' + action.attempt_count + '/' + action.max_attempts + ' 次' : '尚未发起请求' }}<template v-if="action.next_retry_at">
                  · {{ countdown(action.next_retry_at) }}
                </template>
              </p>
              <p
                v-if="action.error_code"
                class="tasks__step-error"
              >
                {{ errorText(action.error_code) }}
              </p>
            </li>
          </ol>
        </details>
        <section
          v-if="snapshot.run.result"
          class="tasks__result"
          aria-label="任务交付结果"
        >
          <div class="tasks__section-heading">
            <h2>执行结果</h2><span>{{ status === 'completed' ? '已完成' : '已保存内容' }}</span>
          </div><div class="tasks__result-body">
            {{ snapshot.run.result }}
          </div>
        </section>
      </template>
      <p
        v-if="error"
        class="tasks__error"
        role="alert"
      >
        {{ error }}
      </p>
      <div
        v-if="connection"
        class="tasks__connection"
        role="status"
      >
        {{ connection }} <button
          type="button"
          class="tasks__link"
          @click="currentId ? read(revision, false) : refreshList()"
        >
          重新读取
        </button>
      </div>
      <p class="tasks__footnote">
        关闭页面不会停止任务。执行依赖本机后端运行；任务和普通问答分开保存。
      </p>
    </main>
  </div>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.tasks {
  display: grid; grid-template-columns: 235px minmax(0, 1fr); min-height: calc(100dvh - 77px);
  &__history { padding: 26px 16px; border-right: 1px solid $line; background: #f2f4ed; display: flex; flex-direction: column; gap: 20px; }
  &__history-heading { color: $muted; font-size: 12px; display: flex; align-items: center; justify-content: space-between; padding: 0 8px; button { background: none; border: 0; color: $green; font-size: 20px; } }
  &__new { border: 1px solid #cad6bf; color: $green; background: #e8eedd; padding: 12px; border-radius: 6px; text-align: left; }
  &__history-item { display: block; color: #657361; border-radius: 6px; padding: 13px 11px; text-decoration: none; font-size: 12px; line-height: 1.7; span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } small { font-size: 10px; color: $muted; } &--active { background: #dfe8d5; color: #294d38; } }
  &__private { margin-top: auto; color: $muted; font-size: 10px; padding: 20px 8px 0; }
  &__workspace { padding: 38px clamp(22px, 5vw, 70px) 30px; min-width: 0; }
  &__intro { padding: 25px 0 32px; h1 { font-size: clamp(28px, 3.2vw, 42px); font-weight: 400; line-height: 1.6; margin: 22px 0; } p { color: $muted; font-size: 13px; line-height: 2; } }
  &__eyebrow { color: #859576; font-size: 9px; letter-spacing: 2px; }
  &__create { padding: 22px; background: white; border: 1px solid #c7d5bc; border-radius: 9px; label { display: block; color: $green; font-size: 13px; margin-bottom: 15px; } textarea { display: block; width: 100%; resize: vertical; border: 0; outline: 0; min-height: 100px; font: inherit; line-height: 1.9; color: $ink; } }
  &__create-footer { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 15px; justify-content: space-between; align-items: center; span { color: $muted; font-size: 11px; } }
  &__primary { border: 0; border-radius: 6px; background: $green; color: white; padding: 12px 18px; font-size: 12px; }
  &__top { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; h1 { font-size: 22px; font-weight: 500; line-height: 1.7; margin: 12px 0 20px; overflow-wrap: anywhere; } }
  &__status { font-size: 11px; padding: 7px 11px; white-space: nowrap; border-radius: 20px; background: #e7eddc; color: $green; &--failed { background: #faeee6; color: #934b40; } &--retry_wait, &--paused { background: #f2eadc; color: #7a613f; } }
  &__controls { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 15px; font-size: 12px; color: $muted; margin: 3px 0 25px; div { display: flex; gap: 8px; } button { color: $green; background: transparent; border: 1px solid $line; padding: 8px 12px; border-radius: 5px; font-size: 12px; } }
  &__waiting { background: #f2eadc; color: #7a613f; padding: 18px; margin-bottom: 20px; display: flex; gap: 14px; border-radius: 6px; font-size: 12px; line-height: 1.9; strong { font-weight: 500; } p { margin-top: 4px; font-size: 11px; } }
  &__waiting-mark { font-size: 23px; }
  &__plan, &__result { background: white; border: 1px solid $line; border-radius: 8px; margin-bottom: 22px; }
  &__section-heading { padding: 20px 24px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid $line; h2 { font-size: 15px; font-weight: 500; } span, small { font-size: 10px; color: $muted; } small { margin-left: 10px; } }
  &__confirm-hint { color: $muted; font-size: 12px; line-height: 1.9; margin: 20px 24px 0; }
  &__steps { list-style: none; padding: 0 24px; }
  &__step { display: flex; gap: 17px; padding: 23px 0; & + & { border-top: 1px solid $line; } }
  &__number { color: #92a184; font-size: 12px; padding-top: 2px; font-variant-numeric: tabular-nums; }
  &__step-main { min-width: 0; flex: 1; }
  &__step-title { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; h3 { font-size: 14px; font-weight: 500; line-height: 1.7; } span { color: $muted; font-size: 10px; } }
  &__instruction { font-size: 12px; color: $muted; line-height: 1.9; margin-top: 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
  &__dependency, &__tool { font-size: 10px; color: $muted; line-height: 1.8; margin-top: 7px; }
  &__tool { color: $green; }
  &__step-progress { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 13px; font-size: 11px; color: $green; small { color: $muted; font-size: 10px; } }
  &__step-state--failed, &__step-error { color: #934b40; }
  &__step-error { margin-top: 8px; font-size: 11px; line-height: 1.8; }
  &__step-result { margin-top: 12px; font-size: 12px; summary { color: $green; cursor: pointer; } div { padding: 13px 0; white-space: pre-wrap; line-height: 1.9; overflow-wrap: anywhere; } }
  &__confirm { border-top: 1px solid $line; padding: 20px 24px; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 14px; p { color: $muted; font-size: 11px; } }
  &__actions { margin: 22px 0; border: 1px solid $line; border-radius: 8px; background: white; padding: 18px 24px; summary { color: $green; font-size: 13px; cursor: pointer; span { color: $muted; font-size: 10px; margin-left: 12px; } } ol { list-style: none; padding: 0; } li { margin-top: 16px; border-top: 1px solid $line; padding-top: 14px; font-size: 12px; line-height: 1.9; div { display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; } strong { font-weight: 500; } span, p { color: $muted; font-size: 11px; } } }
  &__actions-hint { color: $muted; font-size: 11px; line-height: 1.9; margin-top: 14px; }
  &__retry-attempt { margin-top: 9px; }
  &__editor { padding: 20px 24px; border-top: 1px solid $line; label { display: block; color: $green; font-size: 13px; } p { color: $muted; font-size: 11px; line-height: 1.9; margin: 10px 0; } textarea { width: 100%; font: 12px/1.8 monospace; resize: vertical; border: 1px solid $line; border-radius: 5px; padding: 12px; color: $ink; } div { display: flex; gap: 12px; margin-top: 12px; } }
  &__edit, &__editor button:not(.tasks__primary) { border: 1px solid $line; color: $green; background: $paper; padding: 10px 14px; border-radius: 5px; font-size: 12px; }
  &__result-body { padding: 24px; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.95; font-size: 14px; }
  &__empty { padding: 40px 0; color: $muted; font-size: 13px; line-height: 1.9; }
  &__error, &__run-error { color: #934b40; background: #faeee6; padding: 12px 15px; font-size: 12px; line-height: 1.8; border-radius: 5px; margin: 15px 0; overflow-wrap: anywhere; }
  &__connection, &__hint { font-size: 12px; color: $muted; line-height: 1.9; }
  &__link { border: 0; background: none; color: $green; font-size: 12px; text-decoration: underline; padding: 4px; }
  &__footnote { color: $muted; font-size: 10px; margin-top: 24px; line-height: 1.9; }
}
@media (max-width: 760px) { .tasks { grid-template-columns: minmax(0, 1fr); &__history { border-right: 0; border-bottom: 1px solid $line; max-height: 210px; overflow: auto; gap: 12px; padding: 16px; } &__history nav { display: flex; gap: 8px; } &__history-item { min-width: 160px; max-width: 200px; } &__private { display: none; } &__workspace { padding: 24px 18px; } &__top { flex-direction: column; gap: 0; } &__top h1 { font-size: 20px; } &__status { margin-bottom: 20px; } &__section-heading, &__confirm { padding: 18px; } &__steps { padding: 0 18px; } } }
</style>
