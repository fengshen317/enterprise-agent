<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import {
  getSystemLog, listSystemLogs, logComponents, logLevels,
  type LogComponent, type LogDays, type LogLevel, type LogOutcome, type SystemLogDetail, type SystemLogPage,
} from '../api/systemLogs'
import { useAuthStore } from '../stores/auth'
import { copyDiagnosticId } from '../utils/diagnosticClipboard'
import {
  componentLabels, dateTimeLabel, dayLabel, diagnosticMetrics, diagnosticSummary, durationLabel,
  eventTitle, levelLabels, outcomeLabels, sourceDetail, timeLabel, timelineNote,
} from './systemLogLabels'

const PAGE_SIZE = 50
const SEARCH_DELAY_MS = 300
const SEARCH_MAX_LENGTH = 64
const resultOptions = ['error', 'warning', 'success'] as const
const summaries = ['all', ...resultOptions] as const
const ranges = [{ days: 1, label: '最近 24 小时' }, { days: 7, label: '最近 7 天' }, { days: 30, label: '最近 30 天' }] as const
const auth = useAuthStore()
const router = useRouter()
const scope = computed(() => {
  const workspace = auth.activeWorkspace
  return workspace?.role === 'admin' && workspace.status === 'active'
    ? auth.session?.user.id + ':' + workspace.id : ''
})
const days = ref<LogDays>(1)
const component = ref<LogComponent | ''>('')
const outcome = ref<LogOutcome | ''>('')
const level = ref<LogLevel | ''>('')
const query = ref('')
const page = ref<SystemLogPage | null>(null)
const pageIndex = ref(0)
const cursors = ref<(string | undefined)[]>([undefined])
const loading = ref(false)
const listError = ref('')
const detail = ref<SystemLogDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const selectedId = ref('')
const copyFeedback = ref('')
const dialog = ref<HTMLDialogElement | null>(null)
const searchField = ref<HTMLInputElement | null>(null)
const diagnosticField = ref<HTMLElement | null>(null)
let opener: HTMLElement | undefined
let searchTimer: ReturnType<typeof setTimeout> | undefined
let listRevision = 0
let detailRevision = 0

const items = computed(() => page.value?.items ?? [])
const total = computed(() => page.value?.total ?? 0)
const pageCount = computed(() => Math.max(pageIndex.value + 1, Math.ceil(total.value / PAGE_SIZE)))
const pageRange = computed(() => {
  const offset = pageIndex.value * PAGE_SIZE
  return items.value.length ? `${offset + 1}–${offset + items.value.length} / ${total.value} 条` : '0 条'
})
const selected = computed(() => detail.value?.event)
const metrics = computed(() => selected.value ? diagnosticMetrics(selected.value) : [])
const relatedId = computed(() => {
  const event = selected.value
  return event?.run_id || event?.request_id || event?.tool_call_id || event?.task_execution_id
})
const timeline = computed(() => {
  if (!detail.value) return []
  const unique = new Map(detail.value.related.map(event => [event.id, event]))
  unique.set(detail.value.event.id, detail.value.event)
  return [...unique.values()].sort((first, second) => Date.parse(first.occurred_at) - Date.parse(second.occurred_at))
})
const identifiers = computed(() => {
  const event = selected.value
  if (!event) return []
  return [
    { label: '请求 ID', value: event.request_id },
    { label: 'Run ID', value: event.run_id },
    { label: '工具调用 ID', value: event.tool_call_id },
    { label: '工具名称', value: event.tool_name },
    { label: '任务执行 ID', value: event.task_execution_id },
  ].filter(item => item.value)
})

async function handleForbidden(value: unknown): Promise<void> {
  if (!(value instanceof ApiError) || value.status !== 403) return
  const workspaceId = auth.selectedWorkspaceId
  page.value = null
  closeDetail(false)
  try {
    await auth.restore()
    if (!scope.value && auth.selectedWorkspaceId === workspaceId) await router.replace('/chat')
  } catch { /* 保留原始错误提示，避免读取失效权限下的诊断数据。 */ }
}

async function loadPage(index: number): Promise<void> {
  const requestScope = scope.value
  if (!requestScope) return
  const revision = ++listRevision
  loading.value = true
  listError.value = ''
  const current = () => scope.value === requestScope && listRevision === revision
  try {
    const next = await listSystemLogs({
      days: days.value, limit: PAGE_SIZE,
      component: component.value || undefined, outcome: outcome.value || undefined, level: level.value || undefined,
      q: query.value.trim() || undefined, cursor: cursors.value[index],
    })
    if (!current()) return
    page.value = next
    pageIndex.value = index
    cursors.value.splice(index + 1)
    if (next.next_cursor) cursors.value.push(next.next_cursor)
  } catch (value) {
    if (!current()) return
    listError.value = value instanceof Error ? value.message : '暂时无法读取系统日志'
    await handleForbidden(value)
  } finally {
    if (current()) loading.value = false
  }
}

function applyFilters(delayed = false): void {
  clearTimeout(searchTimer)
  listRevision++
  page.value = null
  pageIndex.value = 0
  cursors.value = [undefined]
  listError.value = ''
  loading.value = Boolean(scope.value)
  if (delayed) searchTimer = setTimeout(() => { void loadPage(0) }, SEARCH_DELAY_MS)
  else void loadPage(0)
}

function resetFilters(): void {
  days.value = 1
  component.value = outcome.value = level.value = query.value = ''
}

function selectOutcome(value: typeof summaries[number]): void {
  outcome.value = value === 'all' ? '' : value
  applyFilters()
}

async function loadDetail(id: string): Promise<void> {
  const requestScope = scope.value
  if (!requestScope) return
  const revision = ++detailRevision
  detailLoading.value = true
  detailError.value = ''
  const current = () => scope.value === requestScope && detailRevision === revision
  try {
    const result = await getSystemLog(id)
    if (current()) detail.value = result
  } catch (value) {
    if (!current()) return
    detailError.value = value instanceof Error ? value.message : '暂时无法读取事件详情'
    if (value instanceof ApiError && value.status === 403) listError.value = detailError.value
    await handleForbidden(value)
  } finally {
    if (current()) detailLoading.value = false
  }
}

function openDetail(id: string, event: MouseEvent): void {
  if (!dialog.value || !scope.value) return
  opener = (event.currentTarget as HTMLElement).querySelector('button') ?? undefined
  selectedId.value = id
  detail.value = null
  copyFeedback.value = ''
  if (!dialog.value.open) dialog.value.showModal()
  void loadDetail(id)
}

function closeDetail(restoreFocus = true): void {
  detailRevision++
  if (dialog.value?.open) dialog.value.close()
  selectedId.value = ''
  detail.value = null
  detailLoading.value = false
  detailError.value = ''
  copyFeedback.value = ''
  if (restoreFocus && opener?.isConnected) opener.focus({ preventScroll: true })
  opener = undefined
}

function showRelated(): void {
  const id = relatedId.value
  if (!id) return
  resetFilters()
  days.value = 30
  query.value = id
  closeDetail(false)
  applyFilters()
  void nextTick(() => searchField.value?.focus())
}

async function copyId(): Promise<void> {
  const revision = detailRevision
  const id = selected.value?.id
  if (!id) return
  const result = await copyDiagnosticId(id, diagnosticField.value)
  if (revision === detailRevision) copyFeedback.value = result
}

watch(scope, () => {
  closeDetail(false)
  resetFilters()
  applyFilters()
}, { immediate: true })

onBeforeUnmount(() => {
  clearTimeout(searchTimer)
  listRevision++
  closeDetail(false)
})
</script>

<template>
  <section class="system-logs">
    <header class="system-logs__heading">
      <div>
        <h1>系统日志</h1>
        <p>查询工作空间的运行事件，定位请求、模型和连接器故障。</p>
      </div>
      <RouterLink
        class="system-logs__button"
        to="/admin/members"
      >
        成员与权限
      </RouterLink>
    </header>
    <p
      v-if="!scope"
      class="system-logs__alert"
      role="alert"
    >
      仅当前工作空间的管理员可访问此页面。
    </p>
    <template v-else>
      <div
        class="system-logs__summary"
        role="group"
        aria-label="按结果快速筛选"
      >
        <button
          v-for="item in summaries"
          :key="item"
          type="button"
          class="system-logs__summary-cell"
          :aria-pressed="(outcome || 'all') === item"
          @click="selectOutcome(item)"
        >
          <span class="system-logs__summary-label">
            <span
              class="system-logs__dot"
              :data-outcome="item"
              aria-hidden="true"
            />
            {{ item === 'all' ? '全部事件' : outcomeLabels[item] }}
          </span>
          <strong>{{ page?.counts[item] ?? '—' }}</strong>
        </button>
      </div>
      <section
        class="system-logs__list"
        aria-labelledby="system-log-list-heading"
      >
        <header class="system-logs__list-heading">
          <h2 id="system-log-list-heading">
            事件列表
          </h2>
          <p role="status">
            {{ loading ? '正在读取…' : page ? total + ' 条事件' : '' }}
          </p>
        </header>
        <form
          class="system-logs__filters"
          @submit.prevent="applyFilters()"
        >
          <label class="system-logs__search">
            <span
              class="system-logs__search-icon"
              aria-hidden="true"
            />
            <input
              ref="searchField"
              v-model="query"
              type="search"
              aria-label="搜索诊断编号、Run ID 或错误码"
              placeholder="搜索诊断编号、Run ID 或错误码"
              autocomplete="off"
              :maxlength="SEARCH_MAX_LENGTH"
              @input="applyFilters(true)"
            />
          </label>
          <select
            v-model="days"
            aria-label="时间范围"
            @change="applyFilters()"
          >
            <option
              v-for="item in ranges"
              :key="item.days"
              :value="item.days"
            >
              {{ item.label }}
            </option>
          </select>
          <select
            v-model="component"
            aria-label="事件来源"
            @change="applyFilters()"
          >
            <option value="">
              全部来源
            </option>
            <option
              v-for="item in logComponents"
              :key="item"
              :value="item"
            >
              {{ componentLabels[item] }}
            </option>
          </select>
          <select
            v-model="outcome"
            aria-label="执行结果"
            @change="applyFilters()"
          >
            <option value="">
              全部结果
            </option>
            <option
              v-for="item in resultOptions"
              :key="item"
              :value="item"
            >
              {{ outcomeLabels[item] }}
            </option>
          </select>
          <select
            v-model="level"
            aria-label="日志级别"
            @change="applyFilters()"
          >
            <option value="">
              全部级别
            </option>
            <option
              v-for="item in logLevels"
              :key="item"
              :value="item"
            >
              {{ levelLabels[item] }}
            </option>
          </select>
          <button
            class="system-logs__button"
            type="button"
            @click="resetFilters(); applyFilters()"
          >
            重置
          </button>
        </form>
        <p
          v-if="listError"
          class="system-logs__alert"
          role="alert"
        >
          {{ listError }}
          <button
            class="system-logs__button"
            type="button"
            @click="loadPage(pageIndex)"
          >
            重新加载
          </button>
        </p>
        <table
          class="system-logs__table"
          aria-label="诊断事件"
          :aria-busy="loading"
        >
          <thead>
            <tr>
              <th scope="col">
                时间
              </th><th scope="col">
                来源
              </th><th scope="col">
                事件
              </th><th scope="col">
                结果
              </th><th scope="col">
                耗时
              </th><th scope="col">
                诊断编号
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="event in items"
              :key="event.id"
              class="system-logs__row"
              :data-outcome="event.outcome"
              @click="openDetail(event.id, $event)"
            >
              <td>
                <time
                  class="system-logs__time"
                  :datetime="event.occurred_at"
                >{{ timeLabel(event.occurred_at) }}</time>
                <span class="system-logs__secondary">{{ dayLabel(event.occurred_at) }}</span>
              </td>
              <td>
                {{ componentLabels[event.component] }}
                <span
                  v-if="sourceDetail(event)"
                  class="system-logs__secondary"
                >{{ sourceDetail(event) }}</span>
              </td>
              <td>
                <button
                  class="system-logs__event"
                  type="button"
                  aria-haspopup="dialog"
                >
                  {{ eventTitle(event) }}
                </button>
                <code
                  v-if="event.error_code"
                  class="system-logs__error-code"
                >{{ event.error_code }}</code>
              </td>
              <td>
                <span
                  class="system-logs__badge"
                  :data-outcome="event.outcome"
                >
                  <span
                    class="system-logs__dot"
                    aria-hidden="true"
                  />{{ outcomeLabels[event.outcome] }}
                </span>
              </td>
              <td class="system-logs__time">
                {{ durationLabel(event.duration_ms) }}
              </td>
              <td
                class="system-logs__id"
                :title="event.id"
              >
                <span
                  class="system-logs__arrow"
                  aria-hidden="true"
                >›</span>
                <span class="system-logs__token">{{ event.id }}</span>
              </td>
            </tr>
            <tr v-if="!items.length && !listError">
              <td
                class="system-logs__empty"
                colspan="6"
              >
                {{ loading ? '正在读取日志…' : '没有符合条件的事件' }}
              </td>
            </tr>
          </tbody>
        </table>
        <footer class="system-logs__pagination">
          <span>{{ pageRange }}</span>
          <nav aria-label="事件分页">
            <button
              class="system-logs__button"
              type="button"
              :disabled="loading || pageIndex === 0"
              @click="loadPage(pageIndex - 1)"
            >
              上一页
            </button>
            <span>{{ pageIndex + 1 }} / {{ pageCount }}</span>
            <button
              class="system-logs__button"
              type="button"
              :disabled="loading || !page?.next_cursor"
              @click="loadPage(pageIndex + 1)"
            >
              下一页
            </button>
          </nav>
        </footer>
      </section>

      <dialog
        ref="dialog"
        class="system-logs__drawer"
        aria-labelledby="system-log-detail-heading"
        @click.self="closeDetail()"
        @cancel.prevent="closeDetail()"
      >
        <header class="system-logs__drawer-header">
          <div>
            <p
              v-if="selected"
              class="system-logs__breadcrumb"
            >
              {{ componentLabels[selected.component] }}{{ sourceDetail(selected) ? ' / ' + sourceDetail(selected) : '' }}
            </p>
            <h2 id="system-log-detail-heading">
              {{ selected ? eventTitle(selected) : '事件详情' }}
            </h2>
            <div
              v-if="selected"
              class="system-logs__meta"
            >
              <span
                class="system-logs__badge"
                :data-outcome="selected.outcome"
              >
                <span
                  class="system-logs__dot"
                  aria-hidden="true"
                />{{ outcomeLabels[selected.outcome] }}
              </span>
              <time :datetime="selected.occurred_at">{{ dateTimeLabel(selected.occurred_at) }}</time>
              <span v-if="selected.duration_ms !== null">· {{ durationLabel(selected.duration_ms) }}</span>
              <span>· {{ levelLabels[selected.level] }}</span>
            </div>
          </div>
          <button
            class="system-logs__button system-logs__close"
            type="button"
            aria-label="关闭事件详情"
            @click="closeDetail()"
          >
            ×
          </button>
        </header>
        <div
          class="system-logs__drawer-body"
          :aria-busy="detailLoading"
        >
          <p
            v-if="detailLoading"
            role="status"
          >
            正在读取事件详情…
          </p>
          <p
            v-else-if="detailError"
            class="system-logs__alert"
            role="alert"
          >
            {{ detailError }}
            <button
              class="system-logs__button"
              type="button"
              @click="loadDetail(selectedId)"
            >
              重新加载
            </button>
          </p>
          <template v-else-if="selected">
            <section
              class="system-logs__diagnosis"
              :data-outcome="selected.outcome"
              aria-label="诊断结果"
            >
              <strong>{{ diagnosticSummary(selected) }}</strong>
              <code
                v-if="selected.error_code"
                class="system-logs__error-code"
              >{{ selected.error_code }}</code>
              <dl
                v-if="metrics.length"
                class="system-logs__metrics"
              >
                <div
                  v-for="metric in metrics"
                  :key="metric.key"
                >
                  <dt>{{ metric.label }}</dt><dd>{{ metric.value }}</dd>
                </div>
              </dl>
            </section>
            <section aria-labelledby="system-log-timeline-heading">
              <h3 id="system-log-timeline-heading">
                关联时间线
              </h3>
              <ol class="system-logs__timeline">
                <li
                  v-for="event in timeline"
                  :key="event.id"
                  :data-outcome="event.outcome"
                >
                  <div class="system-logs__timeline-row">
                    <strong>{{ eventTitle(event) }}</strong>
                    <time :datetime="event.occurred_at">{{ timeLabel(event.occurred_at, true) }}</time>
                  </div>
                  <p v-if="timelineNote(event)">
                    {{ timelineNote(event) }}
                  </p>
                </li>
              </ol>
              <p
                v-if="detail?.related_truncated"
                class="system-logs__truncated"
              >
                关联事件较多，可在列表中继续查询。
              </p>
            </section>
            <section aria-labelledby="system-log-identifiers-heading">
              <h3 id="system-log-identifiers-heading">
                关联标识
              </h3>
              <dl class="system-logs__identifiers">
                <div>
                  <dt>诊断编号</dt><dd ref="diagnosticField">
                    {{ selected.id }}
                  </dd>
                </div>
                <div
                  v-for="item in identifiers"
                  :key="item.label"
                >
                  <dt>{{ item.label }}</dt><dd>{{ item.value }}</dd>
                </div>
              </dl>
            </section>
          </template>
        </div>
        <footer
          v-if="selected"
          class="system-logs__drawer-footer"
        >
          <p
            class="system-logs__feedback"
            role="status"
          >
            {{ copyFeedback }}
          </p>
          <button
            class="system-logs__button"
            type="button"
            @click="copyId"
          >
            复制诊断编号
          </button>
          <button
            v-if="relatedId"
            class="system-logs__button system-logs__button--primary"
            type="button"
            @click="showRelated"
          >
            查看关联事件
          </button>
        </footer>
      </dialog>
    </template>
  </section>
</template>

<style scoped lang="scss" src="./systemLogs.scss"></style>
