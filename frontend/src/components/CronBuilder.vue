<script setup lang="ts">
import { computed, ref, watch } from 'vue'

type Mode = 'daily' | 'weekly' | 'monthly' | 'interval' | 'custom'
type IntervalUnit = 'minute' | 'hour'

interface Weekday {
  value: number
  label: string
}

const props = defineProps<{ modelValue: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const MODES: { value: Mode; label: string }[] = [
  { value: 'daily', label: '每天' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
  { value: 'interval', label: '固定间隔' },
  { value: 'custom', label: '高级 Cron' },
]
const WEEKDAYS: Weekday[] = [
  { value: 1, label: '周一' },
  { value: 2, label: '周二' },
  { value: 3, label: '周三' },
  { value: 4, label: '周四' },
  { value: 5, label: '周五' },
  { value: 6, label: '周六' },
  { value: 0, label: '周日' },
]
const WEEKDAY_ORDER = [1, 2, 3, 4, 5, 6, 0]

const mode = ref<Mode>('daily')
const time = ref('09:00')
const weekdays = ref<number[]>([1])
const monthDay = ref(1)
const intervalValue = ref(30)
const intervalUnit = ref<IntervalUnit>('minute')
const custom = ref('')
let lastEmitted = ''

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function intOr(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) ? parsed : fallback
}

function splitTime(): [number, number] {
  const [hour = 9, minute = 0] = time.value.split(':').map(value => intOr(value, 0))
  return [Math.min(23, Math.max(0, hour)), Math.min(59, Math.max(0, minute))]
}

function normalizeWeekdays(values: number[]): number[] {
  return WEEKDAY_ORDER.filter(day => values.includes(day))
}

function parseValue(value: string): void {
  const parts = value.trim().split(/\s+/)
  if (parts.length !== 5) {
    mode.value = 'custom'
    custom.value = value
    return
  }
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts
  if (month !== '*') {
    mode.value = 'custom'
    custom.value = value
    return
  }
  if (dayOfMonth === '*' && dayOfWeek === '*') {
    if ((hour === '*' || hour.startsWith('*/')) && minute === '0') {
      mode.value = 'interval'
      intervalUnit.value = 'hour'
      intervalValue.value = hour === '*' ? 1 : Math.min(23, Math.max(1, intOr(hour.slice(2), 1)))
      return
    }
    if (minute.startsWith('*/') && hour === '*') {
      mode.value = 'interval'
      intervalUnit.value = 'minute'
      intervalValue.value = Math.min(59, Math.max(1, intOr(minute.slice(2), 1)))
      return
    }
    if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) {
      mode.value = 'daily'
      time.value = `${pad(intOr(hour, 9))}:${pad(intOr(minute, 0))}`
      return
    }
  }
  if (dayOfWeek === '*' && /^\d+$/.test(dayOfMonth) && /^\d+$/.test(minute) && /^\d+$/.test(hour)) {
    mode.value = 'monthly'
    monthDay.value = Math.min(31, Math.max(1, intOr(dayOfMonth, 1)))
    time.value = `${pad(intOr(hour, 9))}:${pad(intOr(minute, 0))}`
    return
  }
  if (dayOfMonth === '*' && /^\d+$/.test(minute) && /^\d+$/.test(hour) && /^(\d)(,\d)*$/.test(dayOfWeek)) {
    mode.value = 'weekly'
    weekdays.value = dayOfWeek === '*'
      ? [...WEEKDAY_ORDER]
      : normalizeWeekdays(dayOfWeek.split(',').map(value => value === '7' ? 0 : intOr(value, 0)))
    time.value = `${pad(intOr(hour, 9))}:${pad(intOr(minute, 0))}`
    return
  }
  mode.value = 'custom'
  custom.value = value
}

function build(): string {
  const [hour, minute] = splitTime()
  if (mode.value === 'daily') return `${minute} ${hour} * * *`
  if (mode.value === 'weekly') {
    const selected = normalizeWeekdays(weekdays.value)
    if (selected.length === 0) return `${minute} ${hour} * * 1`
    return `${minute} ${hour} * * ${selected.length === 7 ? '*' : selected.join(',')}`
  }
  if (mode.value === 'monthly') return `${minute} ${hour} ${Math.min(31, Math.max(1, monthDay.value))} * *`
  if (mode.value === 'interval') {
    const amount = Math.trunc(intervalValue.value)
    if (intervalUnit.value === 'hour') {
      const step = Math.min(23, Math.max(1, Number.isFinite(amount) ? amount : 1))
      return step === 1 ? '0 * * * *' : `0 */${step} * * *`
    }
    const step = Math.min(59, Math.max(1, Number.isFinite(amount) ? amount : 1))
    return `*/${step} * * * *`
  }
  return custom.value.trim()
}

function selectMode(next: Mode): void {
  if (next === mode.value) return
  // 切到高级模式时保留当前可视化结果，避免输入框为空导致保存非法值。
  if (next === 'custom') custom.value = build()
  mode.value = next
}

function emitCurrent(): void {
  const value = build()
  lastEmitted = value
  emit('update:modelValue', value)
}

parseValue(props.modelValue)
watch(() => props.modelValue, value => {
  if (value === lastEmitted) return
  parseValue(value)
})
watch([mode, time, weekdays, monthDay, intervalValue, intervalUnit, custom], emitCurrent, { deep: true })

const preview = computed(() => build())
</script>

<template>
  <section class="cron-builder">
    <div
      class="cron-builder__modes"
      role="tablist"
      aria-label="定时规则"
    >
      <button
        v-for="item in MODES"
        :key="item.value"
        type="button"
        role="tab"
        :aria-selected="mode === item.value"
        :class="{ 'cron-builder__mode--active': mode === item.value }"
        @click="selectMode(item.value)"
      >
        {{ item.label }}
      </button>
    </div>
    <div
      v-if="mode === 'daily'"
      class="cron-builder__row"
    >
      <span>每天</span><input
        v-model="time"
        name="cronTime"
        type="time"
      /><span>执行</span>
    </div>
    <div
      v-else-if="mode === 'weekly'"
      class="cron-builder__row cron-builder__row--wrap"
    >
      <span>每周</span>
      <label
        v-for="day in WEEKDAYS"
        :key="day.value"
        class="cron-builder__check"
      ><input
        v-model="weekdays"
        type="checkbox"
        :value="day.value"
      />{{ day.label }}</label>
      <span>在</span><input
        v-model="time"
        name="cronTime"
        type="time"
      />
    </div>
    <div
      v-else-if="mode === 'monthly'"
      class="cron-builder__row"
    >
      <span>每月</span><select
        v-model.number="monthDay"
        name="cronMonthDay"
      >
        <option
          v-for="day in 31"
          :key="day"
          :value="day"
        >
          {{ day }}
        </option>
      </select><span>日</span><input
        v-model="time"
        name="cronTime"
        type="time"
      />
    </div>
    <div
      v-else-if="mode === 'interval'"
      class="cron-builder__row"
    >
      <span>每</span><input
        v-model.number="intervalValue"
        name="cronInterval"
        type="number"
        min="1"
        :max="intervalUnit === 'hour' ? 23 : 59"
      /><select
        v-model="intervalUnit"
        name="cronIntervalUnit"
      >
        <option value="minute">
          分钟
        </option><option value="hour">
          小时
        </option>
      </select><span>执行</span>
    </div>
    <div
      v-else
      class="cron-builder__row"
    >
      <span>五段 Cron</span><input
        v-model="custom"
        name="cronExpression"
        maxlength="100"
        placeholder="0 9 * * *"
      />
    </div>
    <p class="cron-builder__preview">
      生成的 Cron：<code>{{ preview }}</code>
    </p>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.cron-builder {
  margin-top: 8px; border: 1px solid $line; border-radius: 6px; background: white; padding: 14px;
  &__modes { display: flex; flex-wrap: wrap; gap: 6px; button { border: 1px solid $line; background: $paper; color: $muted; border-radius: 4px; padding: 6px 10px; font-size: 11px; } }
  &__mode--active { border-color: $green !important; background: #eaf0e1 !important; color: $green !important; }
  &__row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 13px; color: $green; font-size: 12px; input, select { width: auto; margin: 0; border: 1px solid $line; border-radius: 4px; padding: 6px 8px; background: $paper; color: $ink; font: inherit; font-size: 12px; } input[type="time"] { width: 110px; } input[type="number"] { width: 72px; } }
  &__row input[name="cronExpression"] { flex: 1; min-width: 180px; }
  &__check { display: flex; align-items: center; gap: 4px; color: $ink; input { width: auto; margin: 0; } }
  &__preview { margin-top: 12px; color: $muted; font-size: 11px; code { color: $green; } }
}
</style>
