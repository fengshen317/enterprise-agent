<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import type { ToolCallStatus } from '../../api/tools'

const props = defineProps<{
  inHeader: boolean
  streaming: boolean
  steps: { title: string; status: string }[]
  agents: { id: string; title?: string; role?: string; output?: string; status: ToolCallStatus; error: string | null }[]
  error: string
  loading: boolean
}>()
const emit = defineEmits<{ retry: [] }>()
const panelId = useId()
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const opened = ref(false)
const hasProgress = computed(() => Boolean(props.steps.length || props.agents.length))
const completedSteps = computed(() => props.steps.filter(step => step.status === 'completed').length)
const runningAgents = computed(() => props.agents.filter(agent => agent.status === 'running').length)
const stepLabels: Record<string, string> = { pending: '待处理', in_progress: '进行中', completed: '已完成' }
const roleLabels: Record<string, string> = { analyst: '分析员', reviewer: '复核员' }
const agentLabels: Record<ToolCallStatus, string> = {
  awaiting_approval: '等待审批', running: '执行中', completed: '已完成',
  failed: '失败', cancelled: '已停止', denied: '已拒绝', expired: '已过期',
}

watch(() => props.inHeader && hasProgress.value, hasHeaderProgress => {
  if (hasHeaderProgress && props.streaming) opened.value = true
  if (!props.inHeader) opened.value = false
}, { immediate: true })

watch(opened, (visible, _, onCleanup) => {
  if (!visible) return
  function dismiss(event: PointerEvent): void {
    if (event.target instanceof Node && !root.value?.contains(event.target)) opened.value = false
  }
  document.addEventListener('pointerdown', dismiss)
  onCleanup(() => document.removeEventListener('pointerdown', dismiss))
})

function closeOnEscape(event: KeyboardEvent): void {
  if (!opened.value) return
  event.stopPropagation()
  opened.value = false
  trigger.value?.focus({ preventScroll: true })
}
</script>

<template>
  <div
    v-if="hasProgress || (inHeader && error)"
    ref="root"
    class="execution-progress"
    :class="{ 'execution-progress--header': inHeader }"
    :aria-label="inHeader ? '本轮执行进度' : undefined"
    :role="inHeader ? 'group' : undefined"
    @keydown.esc="closeOnEscape"
  >
    <button
      v-if="inHeader"
      ref="trigger"
      type="button"
      class="execution-progress__trigger"
      :aria-expanded="opened"
      :aria-controls="panelId"
      @click="opened = !opened"
    >
      <span v-if="steps.length">规划 {{ completedSteps }}/{{ steps.length }}</span>
      <span v-if="agents.length">子 Agent {{ agents.length }}</span>
      <span v-if="!hasProgress">进度读取失败</span>
      <span
        class="execution-progress__chevron"
        aria-hidden="true"
      />
    </button>
    <div
      v-show="!inHeader || opened"
      :id="panelId"
      class="execution-progress__panel"
    >
      <p
        v-if="inHeader && error"
        class="execution-progress__error"
      >
        {{ error }}
        <button
          type="button"
          class="execution-progress__retry"
          :disabled="loading"
          @click="emit('retry')"
        >
          重新读取
        </button>
      </p>
      <section
        v-if="steps.length"
        :aria-label="inHeader ? '任务规划' : '工作计划'"
      >
        <h3>任务规划</h3>
        <ol class="execution-progress__steps">
          <li
            v-for="(step, index) in steps"
            :key="index"
            class="execution-progress__row"
          >
            <span>{{ step.title }}</span>
            <span
              class="execution-progress__status"
              :data-status="step.status"
            >{{ stepLabels[step.status] ?? step.status }}</span>
          </li>
        </ol>
      </section>
      <section
        v-if="agents.length"
        aria-label="子 Agent 协作"
      >
        <h3>子 Agent 协作 <span v-if="runningAgents">· {{ runningAgents }} 个执行中</span></h3>
        <ul class="execution-progress__agents">
          <li
            v-for="agent in agents"
            :key="agent.id"
          >
            <div class="execution-progress__row">
              <span>{{ agent.title || '子任务' }} <span
                v-if="agent.role && roleLabels[agent.role]"
                class="execution-progress__role"
              >· {{ roleLabels[agent.role] }}</span></span>
              <span
                class="execution-progress__status"
                :data-status="agent.status"
              >{{ agentLabels[agent.status] }}</span>
            </div>
            <details v-if="agent.status === 'completed' && agent.output">
              <summary>查看结果</summary>
              <p class="execution-progress__output">
                {{ agent.output }}
              </p>
            </details>
            <p
              v-if="agent.error"
              class="execution-progress__error"
            >
              {{ agent.error }}
            </p>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.execution-progress {
  font-size: 12px;
  color: $ink;

  &--header { position: relative; }
  &__trigger {
    display: flex; align-items: center; gap: 9px; min-height: 36px; padding: 6px 10px;
    border: 1px solid $line; border-radius: 7px; background: $sidebar;
    color: $ink; font: inherit; font-variant-numeric: tabular-nums; white-space: nowrap;
    &:hover, &[aria-expanded="true"] { border-color: $green; }
    span + span:not(.execution-progress__chevron) { border-inline-start: 1px solid $line; padding-inline-start: 9px; }
  }
  &__chevron {
    width: 6px; height: 6px; border-right: 1px solid currentColor; border-bottom: 1px solid currentColor;
    transform: translateY(-2px) rotate(45deg);
  }
  &__trigger[aria-expanded="true"] &__chevron { transform: translateY(1px) rotate(225deg); }
  &--header &__panel {
    position: absolute; z-index: 8; inset: calc(100% + 9px) 0 auto auto;
    width: min(360px, calc(100vw - 44px)); max-height: min(52dvh, 440px);
    overflow: auto; overscroll-behavior: contain; padding: 14px 16px;
    border: 1px solid $line; border-radius: 10px; background: $surface;
    box-shadow: 0 10px 26px rgba($ink, .14);
  }
  h3 { margin: 0 0 8px; color: $ink; font-size: 13px; span { color: $muted; font-size: 11px; font-weight: 400; } }
  section { margin-block: 10px; }
  &--header section:first-child { margin-block-start: 0; }
  &--header section:last-child { margin-block-end: 0; }
  section + section { border-top: 1px solid $line; padding-top: 12px; }
  &__steps, &__agents { padding: 0; margin: 0; list-style: none; }
  &__steps { counter-reset: step; }
  &__steps li {
    &::before {
      counter-increment: step; content: counter(step);
      color: $green; min-width: 1.4em; text-align: center; font-variant-numeric: tabular-nums;
      background: $sidebar; border-radius: 4px;
    }
    > span:first-of-type { flex: 1; }
  }
  li + li { margin-top: 7px; }
  &__row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; line-height: 1.6; overflow-wrap: anywhere; }
  &__role { color: $muted; white-space: nowrap; }
  &__status {
    color: $muted; flex-shrink: 0;
    &[data-status="running"], &[data-status="in_progress"], &[data-status="completed"] { color: $green; }
    &[data-status="failed"], &[data-status="denied"], &[data-status="expired"] { color: $log-error; }
  }
  summary { color: $green; cursor: pointer; margin-top: 4px; }
  &__output, &__error { margin: 5px 0 0; line-height: 1.65; overflow-wrap: anywhere; }
  &__output { white-space: pre-wrap; }
  &__error { color: $log-error; }
  &__retry { border: 0; padding: 0; color: $green; background: none; text-decoration: underline; font: inherit; }
}
</style>
