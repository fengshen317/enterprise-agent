<script setup lang="ts">
import type { Agent } from '../../api/agents'

const props = defineProps<{ agents: Agent[]; modelValue: string | null; disabled?: boolean; current: Agent | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>()
function change(event: Event): void {
  const select = event.target as HTMLSelectElement
  const value = select.value
  select.value = props.modelValue ?? ''
  emit('update:modelValue', value || null)
}
</script>

<template>
  <label class="agent-select">
    <span class="agent-select__icon">{{ current?.icon ?? '≋' }}</span>
    <select
      class="agent-select__control"
      :value="modelValue ?? ''"
      :disabled="disabled"
      aria-label="选择 Agent"
      :title="disabled ? '读取、提交或生成期间不能切换 Agent' : undefined"
      @change="change"
    >
      <option value="">序列助手（内置）</option>
      <option
        v-for="item in agents"
        :key="item.id"
        :value="item.id"
      >
        {{ item.name }}{{ item.visibility === 'private' ? '（私有）' : '' }}
      </option>
    </select>
  </label>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.agent-select {
  display: flex; align-items: center; gap: 6px; border: 1px solid $line; background: $paper;
  border-radius: 7px; padding: 0 7px; min-width: 0;
  &__icon { font-size: 15px; color: $green; }
  &__control { border: 0; background: transparent; font: inherit; font-size: 12px; color: $ink; min-height: 32px; max-width: min(210px, 60vw); }
}
@media (max-width: 760px) { .agent-select__control { min-height: 38px; } }
</style>
