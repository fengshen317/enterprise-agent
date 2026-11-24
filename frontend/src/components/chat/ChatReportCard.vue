<script setup lang="ts">
import { onBeforeUnmount, ref, shallowRef, useId, watch } from 'vue'
import { getChatReport, type ChatReport, type ChatReportRef } from '../../api/chatReports'
import ReportChart from './ReportChart.vue'

const props = defineProps<{ conversationId: string; reportRef: ChatReportRef }>()
const headingId = useId()
const tableId = useId()
const report = shallowRef<ChatReport>()
const error = ref('')
let loadToken = 0

async function load(): Promise<void> {
  const token = ++loadToken
  error.value = ''
  report.value = undefined
  try {
    const result = await getChatReport(props.conversationId, props.reportRef.id)
    if (token === loadToken) report.value = result
  } catch (reason) {
    if (token === loadToken) error.value = reason instanceof Error && reason.message ? reason.message : '请稍后重试'
  }
}

watch([() => props.conversationId, () => props.reportRef.id], load, { immediate: true })
onBeforeUnmount(() => { loadToken++ })
</script>

<template>
  <section
    class="chat-report"
    :aria-labelledby="headingId"
  >
    <h3 :id="headingId">
      {{ report?.title || reportRef.title }}
    </h3>
    <div
      v-if="report"
      class="chat-report__content"
    >
      <ReportChart
        v-for="(chart, index) in report.charts"
        :key="index"
        :report="report"
        :chart="chart"
      />
      <div class="chat-report__data">
        <header class="chat-report__data-heading">
          <h4 :id="tableId">
            数据明细
          </h4>
          <span>{{ report.rows.length }} 行</span>
        </header>
        <div
          v-if="report.columns.length"
          class="chat-report__table"
          role="region"
          :aria-labelledby="tableId"
          tabindex="0"
        >
          <table :aria-labelledby="tableId">
            <thead>
              <tr>
                <th
                  v-for="column in report.columns"
                  :key="column"
                  scope="col"
                >
                  {{ column }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(row, rowIndex) in report.rows"
                :key="rowIndex"
              >
                <td
                  v-for="(cell, columnIndex) in row"
                  :key="columnIndex"
                  :class="{ 'chat-report__number': typeof cell === 'number' }"
                >
                  {{ cell ?? '—' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p
          v-if="!report.rows.length"
          class="chat-report__state"
        >
          没有符合条件的数据。
        </p>
      </div>
      <details class="chat-report__details">
        <summary>计算说明</summary>
        <p v-if="report.explanation">
          {{ report.explanation }}
        </p>
        <h4 v-if="report.source_files.length">
          数据来源
        </h4>
        <ul v-if="report.source_files.length">
          <li
            v-for="(filename, index) in report.source_files"
            :key="index"
          >
            {{ filename }}
          </li>
        </ul>
      </details>
      <details class="chat-report__details">
        <summary>查看执行的 Python 代码</summary>
        <pre tabindex="0"><code>{{ report.code }}</code></pre>
      </details>
    </div>
    <div
      v-else-if="error"
      class="chat-report__state chat-report__state--error"
    >
      <span role="status">报表加载失败。{{ error }}</span>
      <button
        type="button"
        @click="load"
      >
        重试报表
      </button>
    </div>
    <p
      v-else
      class="chat-report__state"
      role="status"
    >
      加载报表…
    </p>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.chat-report {
  min-width: 0;
  margin-top: 16px;
  padding: 14px;
  border: 1px solid $line;
  border-radius: $panel-radius;
  background: $paper;
  color: $ink;
  overflow-wrap: anywhere;

  h3 { margin: 0; font-size: 16px; font-weight: 650; }
  h4 { margin: 0; font-size: 13px; font-weight: 600; }
  &__content { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; margin-top: 12px; }
  &__data-heading { display: flex; align-items: baseline; gap: 8px; margin: 0 0 6px; }
  &__data-heading span { color: $muted; font-size: 12px; }
  &__table {
    max-height: 24rem;
    overflow: auto;
    border: 1px solid $line;
    border-radius: $control-radius;
    background: $surface;

    table { min-width: 100%; width: max-content; border-spacing: 0; font-size: 13px; }
    th, td { max-width: 18rem; padding: 7px 9px; border-bottom: 1px solid $line; text-align: start; }
    th { position: sticky; z-index: 1; top: 0; background: $sidebar; font-weight: 600; }
    tbody tr:nth-child(even) { background: $paper; }
    tbody tr:last-child td { border-bottom: 0; }
    td.chat-report__number { text-align: end; font-variant-numeric: tabular-nums; }
  }
  &__details {
    border-top: 1px solid $line;
    padding-top: 9px;
    font-size: 13px;

    summary { color: $green; cursor: pointer; }
    p { margin: 8px 0; white-space: pre-wrap; }
    ul { margin: 4px 0 0; padding-inline-start: 20px; }
    pre {
      max-height: 22rem;
      margin: 8px 0 0;
      padding: 10px 12px;
      overflow: auto;
      border-radius: $control-radius;
      background: $ink;
      color: $surface;
      font: 12px/1.6 $log-mono;
    }
    code { font: inherit; }
  }
  &__state { margin: 8px 0 0; color: $muted; font-size: 13px; }
  &__state--error { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; color: $log-error; }
  &__state button { padding: 0; border: 0; color: $green; background: none; text-decoration: underline; }
  :is(summary, button, &__table, pre):focus-visible { outline: 2px solid $green; outline-offset: 2px; }
}
</style>
