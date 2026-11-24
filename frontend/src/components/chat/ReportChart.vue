<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, useId } from 'vue'
import type { EChartsType } from 'echarts/core'
import type { ChatReport, ReportChartSpec } from '../../api/chatReports'

const props = defineProps<{ report: ChatReport; chart: ReportChartSpec }>()
const headingId = useId()
const host = ref<HTMLDivElement>()
const state = ref<'loading' | 'ready' | 'error'>('loading')
let instance: EChartsType | undefined
let observer: ResizeObserver | undefined
let alive = true

async function draw(): Promise<void> {
  state.value = 'loading'
  try {
    const { renderReportChart } = await import('../../utils/reportChart')
    await nextTick()
    if (!alive || !host.value) return
    instance = renderReportChart(host.value, props.report, props.chart)
    observer = new ResizeObserver(() => instance?.resize())
    observer.observe(host.value)
    state.value = 'ready'
  } catch {
    observer?.disconnect()
    instance?.dispose()
    instance = undefined
    if (alive) state.value = 'error'
  }
}

onMounted(draw)

onBeforeUnmount(() => {
  alive = false
  observer?.disconnect()
  instance?.dispose()
  instance = undefined
})
</script>

<template>
  <figure class="report-chart">
    <figcaption :id="headingId">
      {{ chart.title }}
    </figcaption>
    <div
      v-show="state !== 'error'"
      class="report-chart__figure"
    >
      <div
        ref="host"
        class="report-chart__canvas"
        role="img"
        :aria-labelledby="headingId"
        :aria-hidden="state !== 'ready'"
      />
      <p
        v-if="state === 'loading'"
        class="report-chart__loading"
        role="status"
      >
        加载图表…
      </p>
    </div>
    <div
      v-if="state === 'error'"
      class="report-chart__error"
    >
      <span role="status">图表加载失败，可查看下方数据明细。</span>
      <button
        type="button"
        @click="draw"
      >
        重试图表
      </button>
    </div>
  </figure>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.report-chart {
  min-width: 0;
  margin: 0;
  padding: 12px 10px 4px;
  border: 1px solid $line;
  border-radius: $control-radius;
  background: $surface;

  figcaption { padding-inline: 4px; color: $ink; font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
  &__figure { position: relative; }
  &__canvas { width: 100%; height: clamp(220px, 30vw, 300px); }
  &__loading { position: absolute; inset: 40% 0 auto; text-align: center; }
  &__error { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; padding: 10px 4px; }
  &__loading, &__error { color: $muted; font-size: 13px; }
  &__error button { padding: 0; border: 0; color: $green; background: none; text-decoration: underline; }
}
</style>
