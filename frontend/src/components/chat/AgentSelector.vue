<script setup lang="ts">
import type { Agent } from '../../api/agents'

defineProps<{ agents: Agent[]; modelValue: string | null; disabled?: boolean; current: Agent | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>()
function change(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
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
    <span
      v-if="disabled"
      class="agent-select__hint"
    >生成中不能切换</span>
  </label>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.agent-select {
  display: flex; align-items: center; gap: 8px; border: 1px solid #cbd7be; background: #f4f7ee;
  border-radius: 20px; padding: 5px 10px 5px 8px; min-width: 0;
  &__icon { font-size: 15px; color: $green; }
  &__control { border: 0; background: transparent; font: inherit; font-size: 12px; color: $ink; max-width: 210px; outline: 0; }
  &__hint { font-size: 10px; color: $muted; white-space: nowrap; }
}
</style>
