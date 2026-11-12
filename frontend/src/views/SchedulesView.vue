<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { RouterLink } from 'vue-router'
import CronBuilder from '../components/CronBuilder.vue'
import { createConversation, listConversations, type Conversation } from '../api/chat'
import { createSchedule, deleteSchedule, listSchedules, runScheduleNow, updateSchedule, type Schedule, type ScheduleInput } from '../api/schedules'

const items = ref<Schedule[]>([])
const conversations = ref<Conversation[]>([])
const loading = ref(false)
const saving = ref(false)
const creatingConversation = ref(false)
const rowBusy = ref('')
const error = ref('')
const notice = ref('')
const showForm = ref(false)
const editingId = ref('')
const form = ref<ScheduleInput>(blankForm())
let alive = true
let revision = 0

const activeConversations = computed(() => conversations.value.filter(item => item.status === 'active'))

function blankForm(): ScheduleInput {
  return { name: '', prompt: '', conversation_id: '', cron_expression: '0 9 * * *', timezone: 'Asia/Shanghai', enabled: true }
}

function resetForm(): void {
  form.value = { ...blankForm(), conversation_id: activeConversations.value[0]?.id ?? '' }
  editingId.value = ''
}

function startCreate(): void {
  if (editingId.value) { cancelEdit(); return }
  showForm.value = !showForm.value
  if (showForm.value) resetForm()
}

function startEdit(item: Schedule): void {
  form.value = {
    name: item.name,
    prompt: item.prompt,
    conversation_id: item.conversation_id,
    cron_expression: item.cron_expression,
    timezone: item.timezone,
    enabled: item.enabled,
  }
  editingId.value = item.id
  showForm.value = true
}

function cancelEdit(): void {
  resetForm()
  showForm.value = false
}

function handleError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(): Promise<void> {
  const token = ++revision
  loading.value = true; error.value = ''
  try {
    const [schedules, list] = await Promise.all([listSchedules(), listConversations()])
    if (!alive || token !== revision) return
    items.value = schedules
    conversations.value = list
    if (!form.value.conversation_id) form.value.conversation_id = activeConversations.value[0]?.id ?? ''
  } catch (value) { if (alive && token === revision) handleError(value) }
  finally { if (alive && token === revision) loading.value = false }
}

async function createConversationForSchedule(): Promise<void> {
  if (creatingConversation.value || saving.value) return
  creatingConversation.value = true; error.value = ''; notice.value = ''
  try {
    const conversation = await createConversation()
    if (!alive) return
    conversations.value = [conversation, ...conversations.value.filter(item => item.id !== conversation.id)]
    form.value.conversation_id = conversation.id
    notice.value = '已新建对话并固定；首次触发后标题会更新为定时问题。'
  } catch (value) { if (alive) handleError(value) }
  finally { creatingConversation.value = false }
}

async function submit(): Promise<void> {
  if (saving.value) return
  if (!form.value.name.trim() || !form.value.prompt.trim() || !form.value.conversation_id || !form.value.cron_expression.trim()) {
    error.value = '请填写名称、定时问题、固定对话和 Cron 表达式'
    return
  }
  saving.value = true; error.value = ''; notice.value = ''
  try {
    if (editingId.value) await updateSchedule(editingId.value, { ...form.value })
    else await createSchedule({ ...form.value })
    if (!alive) return
    notice.value = editingId.value ? '定时任务已更新。' : '定时任务已创建。'
    showForm.value = false
    resetForm()
    await load()
  } catch (value) { if (alive) handleError(value) }
  finally { saving.value = false }
}

async function toggle(item: Schedule): Promise<void> {
  if (rowBusy.value) return
  rowBusy.value = item.id; error.value = ''; notice.value = ''
  try {
    await updateSchedule(item.id, {
      name: item.name,
      prompt: item.prompt,
      conversation_id: item.conversation_id,
      cron_expression: item.cron_expression,
      timezone: item.timezone,
      enabled: !item.enabled,
    })
    if (!alive) return
    notice.value = item.enabled ? '定时任务已停用。' : '定时任务已启用。'
    await load()
  } catch (value) { if (alive) handleError(value) }
  finally { rowBusy.value = '' }
}

async function runNow(item: Schedule): Promise<void> {
  if (rowBusy.value) return
  rowBusy.value = item.id; error.value = ''; notice.value = ''
  try {
    const snapshot = await runScheduleNow(item.id)
    if (!alive) return
    notice.value = '已触发一次运行，结果会写入「' + item.conversation_title + '」。运行状态：' + snapshot.run.status
    await load()
  } catch (value) { if (alive) handleError(value) }
  finally { rowBusy.value = '' }
}

async function remove(item: Schedule): Promise<void> {
  if (rowBusy.value) return
  rowBusy.value = item.id; error.value = ''; notice.value = ''
  try {
    await deleteSchedule(item.id)
    if (!alive) return
    notice.value = '定时任务已删除，已有对话和运行记录保留。'
    if (editingId.value === item.id) cancelEdit()
    await load()
  } catch (value) { if (alive) handleError(value) }
  finally { rowBusy.value = '' }
}

function timeLabel(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '—'
}

function statusLabel(value: string | null): string {
  return value === 'started' ? '已触发' : value === 'skipped' ? '已跳过' : '—'
}

void load()
onBeforeUnmount(() => { alive = false; revision++ })
</script>

<template>
  <section class="schedules">
    <header class="schedules__heading">
      <div>
        <p class="schedules__eyebrow">
          SCHEDULE / AUTOMATION
        </p><h1>定时问答</h1><p class="schedules__description">
          按 Cron 规则向固定对话追加一次真实问答，复用现有规划、工具和子 Agent。
        </p>
      </div>
      <button
        class="schedules__primary"
        type="button"
        :disabled="saving"
        @click="startCreate"
      >
        {{ editingId ? '取消编辑' : showForm ? '收起表单' : '＋ 新建定时任务' }}
      </button>
    </header>
    <p class="schedules__boundary">
      定时任务只属于当前用户和空间，管理员也不能查看或修改他人的任务。触发结果会作为新运行写入固定对话。
    </p>
    <p
      v-if="error"
      class="schedules__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="schedules__notice"
      role="status"
    >
      {{ notice }}
    </p>
    <form
      v-if="showForm"
      class="schedules__form"
      @submit.prevent="submit"
    >
      <h2>{{ editingId ? '编辑定时任务' : '新建定时任务' }}</h2>
      <div class="schedules__fields">
        <label>名称<input
          v-model="form.name"
          name="scheduleName"
          maxlength="100"
          required
          :disabled="saving"
          placeholder="例如：每日晨报"
        /></label>
        <div class="schedules__field">
          <label for="schedule-conversation">固定对话</label>
          <div class="schedules__conversation">
            <select
              id="schedule-conversation"
              v-model="form.conversation_id"
              name="scheduleConversation"
              required
              :disabled="saving || !activeConversations.length"
            >
              <option
                value=""
                disabled
              >
                选择对话
              </option>
              <option
                v-for="item in activeConversations"
                :key="item.id"
                :value="item.id"
              >
                {{ item.title }}
              </option>
            </select><button
              type="button"
              class="schedules__new-conversation"
              :disabled="saving || creatingConversation"
              @click="createConversationForSchedule"
            >
              {{ creatingConversation ? '新建中…' : '＋ 新建对话' }}
            </button>
          </div>
        </div>
        <label>时区<input
          v-model="form.timezone"
          name="scheduleTimezone"
          maxlength="64"
          list="schedule-timezones"
          required
          :disabled="saving"
        /></label>
      </div>
      <div class="schedules__cron">
        <span class="schedules__cron-label">执行时间</span>
        <CronBuilder v-model="form.cron_expression" />
      </div>
      <label class="schedules__prompt">定时问题<textarea
        v-model="form.prompt"
        name="schedulePrompt"
        rows="4"
        maxlength="20000"
        required
        :disabled="saving"
        placeholder="到期后自动提交给 Agent 的问题或目标"
      /></label>
      <p class="schedules__hint">
        可用“执行时间”构建器选择每天、每周、每月或固定间隔，也可以切换“高级 Cron”手写五段表达式。示例：<code>0 9 * * *</code> 每天 09:00；<code>*/30 * * * *</code> 每 30 分钟。
      </p>
      <label class="schedules__check"><input
        v-model="form.enabled"
        name="scheduleEnabled"
        type="checkbox"
        :disabled="saving"
      />创建后立即启用</label>
      <div class="schedules__actions">
        <button
          class="schedules__primary"
          type="submit"
          :disabled="saving || !activeConversations.length"
        >
          {{ saving ? '保存中…' : editingId ? '保存修改' : '保存定时任务' }}
        </button>
        <button
          type="button"
          :disabled="saving"
          @click="cancelEdit"
        >
          取消
        </button>
      </div>
      <p
        v-if="!activeConversations.length"
        class="schedules__hint"
      >
        当前没有可用对话，请先在问答工作台创建对话。
      </p>
    </form>
    <section
      class="schedules__card"
      aria-label="定时任务列表"
    >
      <div class="schedules__toolbar">
        <span>我的定时任务 <strong>{{ items.length }}</strong></span><button
          type="button"
          :disabled="loading"
          @click="load"
        >
          刷新
        </button>
      </div>
      <div
        class="schedules__table-wrap"
        :aria-busy="loading"
      >
        <table class="schedules__table">
          <thead><tr><th>任务</th><th>固定对话</th><th>Cron / 时区</th><th>下次触发</th><th>最近触发</th><th>操作</th></tr></thead>
          <tbody>
            <tr
              v-for="item in items"
              :key="item.id"
            >
              <td>
                <strong>{{ item.name }}</strong><small>{{ item.prompt }}</small><span
                  class="schedules__state"
                  :class="{ 'schedules__state--on': item.enabled }"
                >{{ item.enabled ? '已启用' : '已停用' }}</span>
              </td>
              <td>
                <RouterLink :to="'/chat/' + item.conversation_id">
                  {{ item.conversation_title }}
                </RouterLink>
              </td>
              <td>
                <code>{{ item.cron_expression }}</code><small>{{ item.timezone }}</small>
              </td>
              <td>{{ item.enabled ? timeLabel(item.next_run_at) : '已停用' }}</td>
              <td>
                <span>{{ timeLabel(item.last_run_at) }}</span><small>{{ statusLabel(item.last_status) }}<template v-if="item.last_error_code">
                  · {{ item.last_error_code }}
                </template></small>
              </td>
              <td class="schedules__row-actions">
                <button
                  type="button"
                  :disabled="!!rowBusy"
                  @click="runNow(item)"
                >
                  {{ rowBusy === item.id ? '处理中…' : '运行一次' }}
                </button><button
                  type="button"
                  :disabled="!!rowBusy"
                  @click="startEdit(item)"
                >
                  编辑
                </button><button
                  type="button"
                  :disabled="!!rowBusy"
                  @click="toggle(item)"
                >
                  {{ item.enabled ? '停用' : '启用' }}
                </button><button
                  type="button"
                  :disabled="!!rowBusy"
                  @click="remove(item)"
                >
                  删除
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <p
          v-if="!items.length"
          class="schedules__empty"
        >
          {{ loading ? '正在读取定时任务…' : '还没有定时任务。' }}
        </p>
      </div>
    </section>
    <datalist id="schedule-timezones">
      <option value="Asia/Shanghai" /><option value="UTC" /><option value="Asia/Tokyo" /><option value="Europe/London" /><option value="America/New_York" />
    </datalist>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.schedules {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 22px; h1 { font-size: 28px; font-weight: 500; margin: 13px 0; } }
  &__eyebrow { color: $muted; font-size: 10px; letter-spacing: 2px; }
  &__description, &__hint, &__boundary { color: $muted; font-size: 12px; line-height: 1.9; }
  &__boundary { border-left: 2px solid $line; padding-left: 14px; margin-bottom: 26px; }
  &__primary { background: $green; color: white; border: 0; border-radius: 6px; padding: 11px 17px; }
  &__form, &__card { margin: 22px 0; background: white; border: 1px solid $line; border-radius: 8px; overflow: hidden; }
  &__form { padding: 24px; h2 { margin-bottom: 20px; font-size: 17px; font-weight: 500; } }
  &__fields { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; label { color: $green; font-size: 12px; } input, select { display: block; margin-top: 8px; width: 100%; } }
  &__field label { color: $green; font-size: 12px; }
  &__conversation { display: flex; align-items: flex-end; gap: 8px; select { flex: 1; margin-top: 8px; } button { margin-top: 8px; } }
  &__new-conversation { white-space: nowrap; border: 1px solid $line; background: $paper; border-radius: 5px; padding: 9px 11px; color: $green; font-size: 12px; }
  &__cron { margin-top: 20px; }
  &__cron-label { display: block; color: $green; font-size: 12px; }
  input, select, textarea { border: 1px solid $line; border-radius: 5px; padding: 9px 11px; background: $paper; color: $ink; font: inherit; font-size: 12px; &:focus { outline: 2px solid #92ae7a; outline-offset: 2px; } }
  textarea { display: block; margin-top: 8px; width: 100%; resize: vertical; }
  &__prompt { display: block; margin-top: 20px; color: $green; font-size: 12px; }
  &__check { display: flex; align-items: center; gap: 8px; margin-top: 18px; color: $green; font-size: 12px; input { width: auto; margin: 0; } }
  &__hint { margin-top: 12px; code { color: $green; } }
  &__actions { display: flex; gap: 10px; margin-top: 20px; button:not(.schedules__primary) { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 10px 16px; color: $green; } }
  &__toolbar { display: flex; align-items: center; justify-content: space-between; padding: 20px; span { font-size: 14px; strong { color: $muted; margin-left: 6px; } } button { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 8px 12px; color: $green; font-size: 12px; } }
  &__table-wrap { overflow-x: auto; }
  &__table { width: 100%; border-collapse: collapse; text-align: left; th { background: $paper; color: $muted; font-size: 11px; font-weight: 400; padding: 13px 20px; white-space: nowrap; } td { padding: 17px 20px; border-bottom: 1px solid $line; font-size: 12px; white-space: nowrap; vertical-align: top; } strong { font-weight: 500; } small { display: block; color: $muted; margin-top: 7px; max-width: 280px; white-space: normal; line-height: 1.7; } code { color: $green; } }
  &__state { display: inline-block; margin-top: 8px; padding: 3px 7px; border-radius: 4px; background: #eef0eb; color: $muted; font-size: 10px; &--on { background: #e6f2e9; color: #16745a; } }
  &__row-actions { display: flex; flex-wrap: wrap; gap: 6px; button { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 6px 9px; color: $green; font-size: 11px; } }
  &__error, &__notice { padding: 12px 16px; border-radius: 5px; font-size: 12px; line-height: 1.8; margin-bottom: 18px; }
  &__error { background: #faeee6; color: #934b40; }
  &__notice { background: #eaf0e1; color: $green; }
  &__empty { padding: 24px; color: $muted; font-size: 12px; }
}
@media (max-width: 900px) { .schedules { padding: 24px 16px; &__fields { grid-template-columns: 1fr; } &__heading { align-items: flex-start; flex-direction: column; } } }
</style>
