<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { decideApproval, type Approval } from '../../api/approvals'

const props = defineProps<{ approval: Approval; runId: string }>()
const emit = defineEmits<{ decided: [approval: Approval] }>()
const busy = ref(false)
const error = ref('')
const now = ref(Date.now())
const clock = setInterval(() => { now.value = Date.now() }, 1000)
const remaining = computed(() => Math.max(0, Math.ceil((Date.parse(props.approval.expires_at) - now.value) / 1000)))
const preview = computed(() => Object.entries(props.approval.args_preview).filter(([, value]) => value !== null && value !== ''))
const risks: Record<string, string> = { write: '写入操作', delete: '删除操作', external: '外发操作' }
async function decide(decision: 'approve' | 'reject'): Promise<void> {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { emit('decided', await decideApproval(props.runId, props.approval.id, decision)) }
  catch (value) { error.value = value instanceof Error ? value.message : '审批失败，请重试' }
  finally { busy.value = false }
}
onBeforeUnmount(() => clearInterval(clock))
</script>

<template>
  <section
    class="approval"
    role="alertdialog"
    aria-label="写操作审批"
  >
    <header class="approval__head">
      <div>
        <span class="approval__eyebrow">APPROVAL REQUIRED</span>
        <h3>需要你的批准</h3>
      </div>
      <span class="approval__risk">{{ risks[approval.risk_level] ?? '高风险操作' }}</span>
    </header>
    <p class="approval__tool">
      Agent 请求执行「{{ approval.tool_name }}」
    </p>
    <dl class="approval__preview">
      <template
        v-for="[key, value] in preview"
        :key="key"
      >
        <dt>{{ key }}</dt>
        <dd>{{ String(value) }}</dd>
      </template>
    </dl>
    <p class="approval__hint">
      批准后立即执行并写入审计；{{ remaining }} 秒后自动过期。
    </p>
    <p
      v-if="error"
      class="approval__error"
      role="alert"
    >
      {{ error }}
    </p>
    <div class="approval__actions">
      <button
        type="button"
        class="approval__approve"
        :disabled="busy"
        @click="decide('approve')"
      >
        {{ busy ? '正在提交…' : '批准并执行' }}
      </button>
      <button
        type="button"
        class="approval__reject"
        :disabled="busy"
        @click="decide('reject')"
      >
        拒绝
      </button>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.approval {
  border: 1px solid #e0c9a8; background: #fcf7ec; border-radius: 8px; padding: 14px 16px; margin-bottom: 10px;
  &__head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; h3 { font-size: 14px; margin-top: 5px; color: #684b28; } }
  &__eyebrow { font-size: 9px; letter-spacing: 1.4px; color: #a07a45; }
  &__risk { border: 1px solid #d9b98b; color: #7d5a2e; border-radius: 20px; padding: 4px 9px; font-size: 10px; white-space: nowrap; }
  &__tool { margin: 10px 0 8px; font-size: 12px; color: #5f4a2e; }
  &__preview { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 6px 10px; margin: 0; font-size: 11px; line-height: 1.7; dt { color: #9b7a50; } dd { margin: 0; overflow-wrap: anywhere; color: #57432b; } }
  &__hint { color: #9a7b54; font-size: 10px; margin: 10px 0; }
  &__error { color: #8b493c; font-size: 11px; margin: 8px 0; }
  &__actions { display: flex; gap: 9px; }
  &__approve, &__reject { border: 0; border-radius: 5px; padding: 8px 14px; font-size: 12px; }
  &__approve { background: #6a4e2c; color: #fff8eb; }
  &__reject { background: #f1e6d4; color: #7a5b32; }
  button:disabled { opacity: .55; }
}
</style>
