<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import {
  checkImaAccount, removePersonalAccount, savePersonalAccount,
  type ImaAccountCheckResult, type ImaProbeResult, type PersonalAccount,
} from '../../api/connectors'
import { copyDiagnosticId } from '../../utils/diagnosticClipboard'

const props = defineProps<{
  connectorKey: string
  account: PersonalAccount
  enabled: boolean
  confirmBeforeUnbind?: boolean
}>()
const emit = defineEmits<{ updated: [account: PersonalAccount] }>()
const SAVED_SECRET_PLACEHOLDER = '••••••••'
const CHECK_LABELS = { knowledge_base: '知识库', notes: '笔记' } as const
const CHECK_ERROR_LABELS = new Map([
  ['auth_failed', '凭证验证失败'],
  ['permission_denied', '访问权限不足'],
  ['timeout', '请求超时'],
  ['network_error', '网络连接失败'],
  ['invalid_output', 'ima 响应格式异常'],
  ['upstream_error', 'ima 请求失败'],
])
const formId = useId()
const hintId = useId()
const draft = ref<Record<string, string>>({})
const operation = ref<'update' | 'check' | null>(null)
const busy = computed(() => operation.value !== null)
const error = ref('')
const checkResult = ref<ImaAccountCheckResult | null>(null)
const copyStatus = ref('')
const confirmingUnbind = ref(false)
const canSave = computed(() => props.enabled && props.account.fields.every(field => Boolean(draft.value[field.key]?.trim())))
const canCheck = computed(() => props.connectorKey === 'ima' && props.enabled && props.account.bound)

function checkLabel(result: ImaProbeResult): string {
  if (result.status === 'success') return '连接正常'
  return CHECK_ERROR_LABELS.get(result.error_code ?? '') ?? '检查失败'
}

async function checkConnection(): Promise<void> {
  if (busy.value || !canCheck.value) return
  operation.value = 'check'
  checkResult.value = null
  copyStatus.value = ''
  error.value = ''
  try {
    checkResult.value = await checkImaAccount()
  } catch (value) {
    error.value = value instanceof Error ? value.message : '连接检查失败，请重试'
  } finally {
    operation.value = null
  }
}

async function copyCheckId(id: string, event: MouseEvent): Promise<void> {
  const button = event.currentTarget as HTMLButtonElement
  copyStatus.value = await copyDiagnosticId(id, button.parentElement?.querySelector('code') ?? null)
}

async function update(remove: boolean): Promise<void> {
  if (busy.value || (!remove && !canSave.value)) return
  operation.value = 'update'
  error.value = ''
  try {
    const account = remove
      ? await removePersonalAccount(props.connectorKey)
      : await savePersonalAccount(props.connectorKey, draft.value)
    draft.value = {}
    checkResult.value = null
    copyStatus.value = ''
    confirmingUnbind.value = false
    emit('updated', account)
  } catch (value) {
    error.value = value instanceof Error ? value.message : '账号设置失败，请重试'
  } finally {
    operation.value = null
  }
}
</script>

<template>
  <section class="personal-account">
    <div class="personal-account__header">
      <div>
        <h3>我的账号</h3>
        <p
          class="personal-account__status"
          role="status"
        >
          {{ account.bound ? '凭证已保存' : '尚未绑定' }} · 仅在当前工作空间使用
        </p>
      </div>
      <a
        v-if="enabled"
        :href="account.guide_url"
        target="_blank"
        rel="noopener noreferrer"
      >获取授权凭证 ↗</a>
    </div>
    <p
      v-if="connectorKey === 'qq_mail' && enabled"
      class="personal-account__hint"
    >
      只读访问收件箱。首次在问答中使用时验证 IMAP 授权码。
    </p>
    <form
      v-if="enabled"
      :id="formId"
      class="personal-account__form"
      @submit.prevent="update(false)"
    >
      <label
        v-for="field in account.fields"
        :key="field.key"
        class="personal-account__field"
      >
        {{ field.label }}
        <input
          v-model="draft[field.key]"
          :type="field.secret ? 'password' : field.key === 'email' ? 'email' : 'text'"
          :placeholder="field.secret && account.bound ? SAVED_SECRET_PLACEHOLDER : undefined"
          :aria-describedby="account.bound ? hintId : undefined"
          :autocomplete="field.secret ? 'new-password' : 'off'"
          :disabled="busy"
          maxlength="4096"
          required
        />
      </label>
      <p
        v-if="account.bound && !confirmingUnbind"
        :id="hintId"
        class="personal-account__hint"
      >
        重新填写各字段可替换凭证。
      </p>
    </form>
    <div
      v-if="enabled || account.bound"
      class="personal-account__actions"
    >
      <button
        v-if="enabled"
        :form="formId"
        type="submit"
        class="personal-account__button personal-account__button--primary"
        :disabled="busy || !canSave"
      >
        {{ account.bound ? '替换凭证' : '绑定我的账号' }}
      </button>
      <button
        v-if="canCheck"
        type="button"
        class="personal-account__button"
        :disabled="busy"
        @click="checkConnection"
      >
        {{ operation === 'check' ? '正在检查…' : '检查连接' }}
      </button>
      <button
        v-if="account.bound"
        type="button"
        class="personal-account__button"
        :disabled="busy"
        @click="confirmBeforeUnbind ? confirmingUnbind = true : update(true)"
      >
        解除绑定
      </button>
    </div>
    <div
      v-if="confirmingUnbind && account.bound"
      class="personal-account__confirmation"
    >
      <p>解除绑定后，问答无法继续读取此账号。</p>
      <div class="personal-account__actions">
        <button
          type="button"
          class="personal-account__button"
          :disabled="busy"
          @click="confirmingUnbind = false"
        >
          取消
        </button>
        <button
          type="button"
          class="personal-account__button personal-account__button--danger"
          :disabled="busy"
          @click="update(true)"
        >
          确认解除绑定
        </button>
      </div>
    </div>
    <div
      v-if="canCheck && checkResult"
      class="personal-account__checks"
    >
      <ul
        class="personal-account__results"
        aria-label="ima 连通性结果"
        aria-live="polite"
      >
        <li
          v-for="(result, key) in checkResult"
          :key="key"
        >
          <span>{{ CHECK_LABELS[key] }} · {{ checkLabel(result) }}</span>
          <div
            v-if="result.status === 'error' && result.diagnostic_id"
            class="personal-account__diagnostic"
          >
            <code>{{ result.diagnostic_id }}</code>
            <button
              type="button"
              class="personal-account__copy"
              :aria-label="`复制${CHECK_LABELS[key]}检查诊断编号`"
              @click="copyCheckId(result.diagnostic_id, $event)"
            >
              复制诊断编号
            </button>
          </div>
        </li>
      </ul>
      <p
        class="personal-account__hint"
        role="status"
      >
        {{ copyStatus }}
      </p>
    </div>
    <p
      v-if="error"
      class="personal-account__error"
      role="alert"
    >
      {{ error }}
    </p>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.personal-account {
  border-block: 1px solid $line;
  font-size: 0.9rem;
  &__header {
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline;
    gap: 0.5rem 0.75rem;
    h3 { font-size: 0.95rem; }
    a { color: $green; text-underline-offset: 0.2em; white-space: nowrap; }
  }
  &__status, &__hint { color: $muted; font-size: 0.82rem; line-height: 1.5; }
  &__status { margin-top: 0.35rem; }
  &__form { display: grid; gap: 0.65rem; margin-top: 0.85rem; }
  &__field {
    display: grid; gap: 0.35rem; min-width: 0; font-weight: 500;
    input {
      width: 100%; min-width: 0; min-height: 38px; padding: 0.55rem 0.75rem;
      border: 1px solid $line; border-radius: 6px; color: $ink; font: inherit;
      &::placeholder { color: $muted; opacity: 1; letter-spacing: 0.2em; }
      &:focus-visible { outline: 2px solid $green; outline-offset: 1px; }
    }
  }
  &__actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.85rem; }
  &__button {
    min-height: 36px; padding: 0.45rem 0.8rem; border: 1px solid $line; border-radius: 7px;
    background: $surface; color: $ink;
    &:hover:not(:disabled) { background: $paper; }
    &:disabled { cursor: default; }
    &--primary {
      background: $green; border-color: $green; color: #fff;
      &:hover:not(:disabled) { background: $ink; }
    }
    &--danger { color: #934b40; }
  }
  &__confirmation {
    margin-top: .75rem; padding: .65rem .75rem; border: 1px solid $line; border-radius: 7px;
    background: $paper;
    p { margin: 0; }
  }
  &__checks { margin-top: 0.75rem; }
  &__results { display: grid; gap: 0.45rem; margin: 0; padding: 0; list-style: none; }
  &__diagnostic {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.35rem 0.65rem;
    margin-top: 0.2rem; color: $muted; font-size: 0.82rem;
    code { overflow-wrap: anywhere; }
  }
  &__copy {
    border: 0; padding: 0; background: transparent; color: $green;
    font: inherit; text-decoration: underline; text-underline-offset: 0.2em;
  }
  &__error { margin-top: 0.6rem; color: #934b40; overflow-wrap: anywhere; }
}
</style>
