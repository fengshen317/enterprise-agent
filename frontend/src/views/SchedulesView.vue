<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import CronBuilder from '../components/CronBuilder.vue'
import { createConversation, listConversations, type Conversation } from '../api/chat'
import {
  createSchedule, deleteSchedule, getSchedule, listSchedules, runScheduleNow, scheduleCalendars,
  updateSchedule, updateScheduleEnabled, updateScheduleWeChatPush, type Schedule, type ScheduleInput,
} from '../api/schedules'

const pageSize = 20
const excerptLength = 120

const route = useRoute()
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
const editor = ref<HTMLFormElement>()
const page = ref(0)
const hasNextPage = ref(false)
let alive = true
let revision = 0

const selectedSchedule = computed(() => typeof route.query.schedule === 'string' ? route.query.schedule : '')
const activeConversations = computed(() => conversations.value.filter(item => item.status === 'active'))

function blankForm(): ScheduleInput {
  return { name: '', prompt: '', conversation_id: '', cron_expression: '0 9 * * *', timezone: 'Asia/Shanghai',
    calendar_mode: 'all_days', enabled: true, push_to_wechat: false }
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
    calendar_mode: item.calendar_mode,
    revision: item.revision,
    enabled: item.enabled,
    push_to_wechat: item.push_to_wechat,
  }
  editingId.value = item.id
  showForm.value = true
  void nextTick(() => editor.value?.scrollIntoView?.({ block: 'start' }))
}

function cancelEdit(): void {
  resetForm()
  showForm.value = false
}

function handleError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(targetPage = page.value): Promise<void> {
  const token = ++revision
  loading.value = true; error.value = ''
  try {
    const [schedules, list] = await Promise.all([
      selectedSchedule.value ? getSchedule(selectedSchedule.value).then(item => [item]) : listSchedules(targetPage * pageSize, pageSize + 1),
      listConversations(),
    ])
    if (!alive || token !== revision) return
    if (!schedules.length && targetPage > 0) {
      await load(targetPage - 1)
      return
    }
    items.value = schedules.slice(0, pageSize)
    hasNextPage.value = schedules.length > pageSize
    page.value = targetPage
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
    await updateScheduleEnabled(item.id, !item.enabled)
    if (!alive) return
    notice.value = item.enabled ? '定时任务已停用。' : '定时任务已启用。'
    await load()
  } catch (value) { if (alive) handleError(value) }
  finally { rowBusy.value = '' }
}

async function toggleWeChatPush(item: Schedule): Promise<void> {
  if (rowBusy.value) return
  rowBusy.value = item.id; error.value = ''; notice.value = ''
  try {
    await updateScheduleWeChatPush(item.id, !item.push_to_wechat)
    if (!alive) return
    notice.value = item.push_to_wechat ? '已关闭微信推送。' : '已开启微信推送。'
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

function timeLabel(value: string | null, timezone: string): string {
  return value ? new Date(value).toLocaleString('zh-CN', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }) : '—'
}

function statusLabel(value: string | null): string {
  return value === 'started' ? '已触发' : value === 'skipped' ? '已跳过' : '尚未触发'
}

function excerpt(prompt: string): string {
  const clean = prompt.trim().replace(/\s+/g, ' ')
  return clean.length > excerptLength ? clean.slice(0, excerptLength) + '…' : clean
}

watch(selectedSchedule, () => {
  items.value = []
  page.value = 0
  hasNextPage.value = false
  cancelEdit()
  void load()
}, { immediate: true })
onBeforeUnmount(() => { alive = false; revision++ })
</script>

<template>
  <section class="schedules">
    <header class="schedules__heading">
      <div>
        <h1>定时任务</h1><p class="schedules__description">
          按设定时间运行 Agent，并把结果写入指定对话。
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
      ref="editor"
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
      </div>
      <div class="schedules__cron">
        <span class="schedules__cron-label">执行时间</span>
        <CronBuilder v-model="form.cron_expression" />
        <div class="schedules__fields">
          <label>时区<input
            v-model="form.timezone"
            name="scheduleTimezone"
            maxlength="64"
            list="schedule-timezones"
            required
            :disabled="saving"
          /></label>
          <label>日期筛选<select
            v-model="form.calendar_mode"
            name="scheduleCalendar"
            :disabled="saving"
          >
            <option
              v-for="(label, mode) in scheduleCalendars"
              :key="mode"
              :value="mode"
            >{{ label }}</option>
          </select></label>
        </div>
        <p
          v-if="form.calendar_mode === 'cn_workday'"
          class="schedules__hint"
        >
          按北京时间跳过放假日；调休上班日仍须匹配执行时间。要包含周末调休，频率请选择每天。
        </p>
        <p
          v-else-if="form.calendar_mode === 'cn_a_share'"
          class="schedules__hint"
        >
          按北京时间跳过周末和沪深交易所公告的休市日。待下一年休市公告补齐后，自动恢复计算。
        </p>
      </div>
      <label class="schedules__prompt">任务指令<textarea
        v-model="form.prompt"
        name="schedulePrompt"
        rows="4"
        maxlength="20000"
        required
        :disabled="saving"
        placeholder="到期后自动提交给 Agent 的问题或目标"
      /></label>
      <label class="schedules__check"><input
        v-model="form.enabled"
        name="scheduleEnabled"
        type="checkbox"
        :disabled="saving"
      />启用自动触发</label>
      <label class="schedules__check"><input
        v-model="form.push_to_wechat"
        name="schedulePushToWeChat"
        type="checkbox"
        :disabled="saving"
      />推送结果到微信</label>
      <p class="schedules__hint">
        推送使用已绑定的微信账号。
      </p>
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
        <span>{{ selectedSchedule ? '所选定时任务' : '我的定时任务' }} <strong>{{ items.length }}</strong></span>
        <RouterLink
          v-if="selectedSchedule"
          to="/schedules"
        >
          查看全部任务
        </RouterLink>
        <button
          type="button"
          :disabled="loading"
          @click="load()"
        >
          刷新
        </button>
      </div>
      <div
        class="schedules__results"
        :aria-busy="loading"
      >
        <article
          v-for="item in items"
          :key="item.id"
          class="schedules__item"
        >
          <div class="schedules__item-main">
            <div class="schedules__identity">
              <div class="schedules__item-heading">
                <h3>{{ item.name }}</h3>
                <span
                  class="schedules__state"
                  :class="{
                    'schedules__state--on': item.enabled && !item.calendar_waiting_year,
                    'schedules__state--waiting': item.calendar_waiting_year,
                  }"
                >
                  {{ item.calendar_waiting_year ? '等待日历' : item.enabled ? '已启用' : '已停用' }}
                </span>
              </div>
              <p
                v-if="!selectedSchedule"
                class="schedules__excerpt"
              >
                {{ excerpt(item.prompt) }}
              </p>
            </div>
            <dl class="schedules__timing">
              <div>
                <dt>下次触发</dt>
                <dd v-if="item.calendar_waiting_year">
                  等待 {{ item.calendar_waiting_year }} 年日历
                </dd>
                <dd v-else>
                  {{ timeLabel(item.next_run_at, item.timezone) }}
                </dd>
              </div>
              <div>
                <dt>日期筛选</dt>
                <dd>{{ scheduleCalendars[item.calendar_mode] }}</dd>
              </div>
            </dl>
          </div>
          <div class="schedules__item-footer">
            <RouterLink
              class="schedules__conversation-link"
              :to="'/chat/' + item.conversation_id"
            >
              {{ item.conversation_title || '打开固定对话' }} ↗
            </RouterLink>
            <div class="schedules__row-actions">
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
                @click="toggleWeChatPush(item)"
              >
                {{ item.push_to_wechat ? '关闭微信推送' : '推送到微信' }}
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
            </div>
          </div>
          <details
            class="schedules__details"
            :open="selectedSchedule === item.id"
          >
            <summary>任务详情</summary>
            <dl class="schedules__details-grid">
              <div><dt>Cron / 时区</dt><dd><code>{{ item.cron_expression }}</code> · {{ item.timezone }}</dd></div>
              <div>
                <dt>最近触发</dt>
                <dd>
                  {{ statusLabel(item.last_status) }}
                  <template v-if="item.last_run_at">
                    · {{ timeLabel(item.last_run_at, item.timezone) }}
                  </template>
                  <template v-if="item.last_error_code">
                    · {{ item.last_error_code }}
                  </template>
                </dd>
              </div>
              <div><dt>微信推送</dt><dd>{{ item.push_to_wechat ? '已开启' : '已关闭' }}</dd></div>
            </dl>
            <h4>任务指令</h4>
            <p class="schedules__full-prompt">
              {{ item.prompt }}
            </p>
          </details>
        </article>
        <p
          v-if="!items.length && !error"
          class="schedules__empty"
        >
          {{ loading ? '正在读取定时任务…' : selectedSchedule ? '当前空间找不到此任务。' : '还没有定时任务。' }}
        </p>
      </div>
      <div
        v-if="!selectedSchedule && (page || hasNextPage)"
        class="schedules__pagination"
        aria-label="定时任务分页"
      >
        <button
          type="button"
          :disabled="!page || loading || !!rowBusy"
          @click="load(page - 1)"
        >
          上一页
        </button>
        <span>第 {{ page + 1 }} 页</span>
        <button
          type="button"
          :disabled="!hasNextPage || loading || !!rowBusy"
          @click="load(page + 1)"
        >
          下一页
        </button>
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
  max-width: 1240px;
  margin: 0 auto;
  padding: 40px clamp(18px, 4vw, 54px) 56px;

  &__heading, &__toolbar, &__item-heading, &__item-footer, &__pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  &__heading {
    margin-bottom: 28px;

    h1 { margin: 0; font-size: clamp(27px, 4vw, 40px); letter-spacing: -.04em; }
  }

  &__description { margin: 8px 0 0; color: $muted; font-size: 14px; line-height: 1.6; }

  button {
    min-height: 34px;
    padding: 6px 12px;
    border: 1px solid $line;
    border-radius: 6px;
    background: white;
    color: $green;
    font: inherit;
    font-size: 13px;
    white-space: nowrap;

    &:enabled:hover { background: $paper; }
    &:disabled { opacity: .55; }
  }

  button.schedules__primary {
    min-height: 40px;
    padding-inline: 16px;
    border-color: $green;
    background: $green;
    color: white;

    &:enabled:hover { filter: brightness(1.08); background: $green; }
  }

  &__form, &__card { margin: 0 0 22px; border: 1px solid $line; border-radius: 12px; background: white; }
  &__form { padding: clamp(16px, 3vw, 25px); scroll-margin-top: 20px; }
  &__form h2 { margin: 0 0 20px; font-size: 19px; }

  &__fields {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px 20px;

    input, select { display: block; width: 100%; margin-top: 6px; }
  }

  &__field { min-width: 0; }
  &__fields label, &__prompt, &__cron-label, &__check { color: $green; font-size: 13px; font-weight: 600; }
  &__conversation { display: flex; align-items: flex-end; gap: 8px; }
  &__conversation select { flex: 1; min-width: 0; }
  &__cron { padding: 15px; margin: 20px 0; border: 1px solid $line; border-radius: 8px; background: $paper; }
  &__cron-label { display: block; margin-bottom: 8px; }
  &__cron &__fields { margin-top: 14px; }

  input, select, textarea {
    border: 1px solid $line;
    border-radius: 6px;
    padding: 9px 10px;
    background: white;
    color: $ink;
    font: inherit;
    font-size: 13px;
  }

  :is(input, select, textarea, button, summary, a):focus-visible { outline: 2px solid $green; outline-offset: 2px; }
  textarea { display: block; width: 100%; min-height: 112px; margin-top: 6px; resize: vertical; line-height: 1.65; }
  &__check { display: flex; align-items: center; width: fit-content; gap: 8px; margin-top: 14px; }
  &__check input { margin: 0; accent-color: $green; }
  &__hint { margin: 8px 0 0; color: $muted; font-size: 12px; line-height: 1.6; }
  &__actions { display: flex; gap: 10px; margin-top: 18px; }

  &__toolbar {
    padding: 13px 20px;
    border-bottom: 1px solid $line;
    font-size: 14px;

    strong { margin-left: 5px; color: $muted; font-size: 12px; }
    a { margin-left: auto; }
  }

  &__item {
    padding: 18px 20px 13px;

    & + & { border-top: 1px solid $line; }
  }

  &__item-main {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(260px, .85fr);
    gap: 18px 24px;
  }

  &__item-heading {
    justify-content: flex-start;
    flex-wrap: wrap;
    gap: 6px 10px;

    h3 { margin: 0; font-size: 16px; overflow-wrap: anywhere; }
  }

  &__excerpt {
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    margin: 7px 0 0;
    color: $muted;
    font-size: 13px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  &__state {
    padding: 3px 7px;
    border-radius: 5px;
    background: $paper;
    color: $muted;
    font-size: 11px;
    white-space: nowrap;

    &--on { background: #e6f2e9; color: #166b51; }
    &--waiting { background: #f9eed9; color: #785121; }
  }

  &__timing, &__details-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin: 0;

    dt { margin-bottom: 5px; color: $muted; font-size: 12px; }
    dd { margin: 0; font-size: 13px; line-height: 1.55; overflow-wrap: anywhere; }
  }

  &__timing { font-variant-numeric: tabular-nums; }
  &__item-footer { padding-top: 13px; }
  &__conversation-link {
    min-width: 0;
    overflow: hidden;
    color: $green;
    font-size: 13px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__row-actions {
    display: flex;
    flex: 0 0 auto;
    flex-wrap: wrap;
    gap: 6px;

    button:first-child { background: $paper; font-weight: 600; }
    button:last-child { color: #8d4937; }
  }

  &__details {
    margin-top: 12px;
    font-size: 13px;

    summary { width: fit-content; cursor: pointer; color: $green; }
    h4 { margin: 12px 0 6px; font-size: 13px; }
  }

  &__details-grid { margin-top: 10px; padding: 12px; border-radius: 6px; background: $paper; }
  &__full-prompt { margin: 0; max-height: 320px; overflow: auto; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
  &__pagination { justify-content: flex-end; padding: 12px 20px; border-top: 1px solid $line; font-size: 13px; }
  &__error, &__notice { padding: 12px 16px; border-radius: 6px; font-size: 13px; line-height: 1.6; }
  &__error { background: #faeee6; color: #934b40; }
  &__notice { background: #eaf0e1; color: $green; }
  &__empty { padding: 24px; color: $muted; font-size: 13px; }
}

@media (max-width: 950px) {
  .schedules {
    &__item-main { grid-template-columns: 1fr; gap: 12px; }
    &__item-footer { flex-direction: column; align-items: flex-start; }
    &__row-actions, &__conversation-link { max-width: 100%; }
  }
}

@media (max-width: 600px) {
  .schedules {
    padding-block: 24px;
    &__fields, &__details-grid { grid-template-columns: minmax(0, 1fr); }
    &__heading { align-items: flex-start; flex-direction: column; margin-bottom: 18px; }
    &__toolbar, &__item { padding-inline: 13px; }
  }
}
</style>
