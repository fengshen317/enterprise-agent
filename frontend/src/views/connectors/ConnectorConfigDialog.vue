<script setup lang="ts">
import { toDataURL } from 'qrcode'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ApiError } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import { useConnectorsStore } from '../../stores/connectors'
import YangJiBaoHoldings from './YangJiBaoHoldings.vue'
import {
  getConnector,
  updateConnectorConfig,
  setConnectorEnabled,
  testConnector,
  authenticateConnector,
  getConnectorAuthStatus,
  connectorHealthLabel,
  connectorTime,
  type AuthResult,
  type ConnectorDetail,
  type ConnectorTestResult,
} from '../../api/connectors'

const props = defineProps<{ connectorKey: string }>()
const emit = defineEmits<{ close: [] }>()
const auth = useAuthStore()
const store = useConnectorsStore()
const isAdmin = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
const isYangJiBao = computed(() => props.connectorKey === 'yangjibao')

const detail = ref<ConnectorDetail | null>(null)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const changingEnabled = ref(false)
const authenticating = ref(false)
const error = ref('')
const testResult = ref<ConnectorTestResult | null>(null)
const authResult = ref<AuthResult | null>(null)
const qrImage = ref('')
const qrError = ref('')

const configDraft = ref<Record<string, string>>({})
const AUTH_POLL_MS = 2000
const AUTH_TIMEOUT_MS = 5 * 60 * 1000
const QR_WIDTH_PX = 220
let generation = 0
let stopped = false
let pollTimer: ReturnType<typeof setTimeout> | undefined
let expiresAt = 0
const trustedAuthUrl = computed(() => {
  try {
    const url = new URL(authResult.value?.auth_url ?? '')
    return url.protocol === 'https:' ? url.href : ''
  } catch { return '' }
})
const authHost = computed(() => trustedAuthUrl.value ? new URL(trustedAuthUrl.value).hostname : '')
const qrContent = computed(() =>
  isYangJiBao.value && authenticating.value && authResult.value?.state === 'awaiting_user'
    ? authResult.value.qr_content ?? '' : '',
)
const canStartAuth = computed(() => detail.value?.enabled
  && (isYangJiBao.value ? detail.value.auth_action !== 'configure' : detail.value.auth_action === 'authorize' || authResult.value))
const authButtonLabel = computed(() => {
  if (!isYangJiBao.value) return authenticating.value ? '授权中…' : '去授权'
  if (authenticating.value) return qrContent.value ? '等待扫码…' : '正在获取二维码…'
  if (authResult.value?.state === 'expired' || authResult.value?.state === 'failed') return '重新获取二维码'
  return detail.value?.auth_status === 'authorized' ? '重新扫码' : '扫码连接'
})
const authLabels: Record<AuthResult['state'], string> = {
  starting: '正在准备授权', awaiting_user: '等待浏览器授权', completed: '授权已完成',
  expired: '授权已过期，请重新发起', failed: '授权失败，请重试',
}

watch(qrContent, async (content, _, onCleanup) => {
  let active = true
  onCleanup(() => { active = false })
  qrImage.value = ''
  qrError.value = ''
  if (!content) return
  try {
    const image = await toDataURL(content, { errorCorrectionLevel: 'M', margin: 1, width: QR_WIDTH_PX })
    if (active) qrImage.value = image
  } catch {
    if (active) qrError.value = '二维码生成失败，请关闭后重新扫码。'
  }
})

function current(version: number): boolean { return !stopped && version === generation }

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(): Promise<void> {
  const version = generation
  loading.value = true
  error.value = ''
  try {
    const result = await getConnector(props.connectorKey)
    if (!current(version)) return
    detail.value = result
    configDraft.value = { ...detail.value.config_values }
    // 密钥字段初始化为空（留空表示不修改）
    for (const f of detail.value.config_fields) {
      if (f.is_secret) {
        configDraft.value[f.key] = ''
      }
    }
  } catch (value) {
    if (current(version)) showError(value)
  } finally {
    if (current(version)) loading.value = false
  }
}

async function save(): Promise<void> {
  if (saving.value || !detail.value || !isAdmin.value) return
  const version = generation
  saving.value = true
  error.value = ''
  testResult.value = null
  try {
    const nonSecret: Record<string, unknown> = {}
    const secrets: Record<string, string> = {}
    for (const f of detail.value.config_fields) {
      const val = configDraft.value[f.key] ?? ''
      if (f.is_secret) {
        if (val) secrets[f.key] = val
      } else {
        nonSecret[f.key] = val
      }
    }
    const result = await updateConnectorConfig(props.connectorKey, nonSecret, secrets)
    if (!current(version)) return
    detail.value = result
    configDraft.value = { ...detail.value.config_values }
    for (const f of detail.value.config_fields) {
      if (f.is_secret) configDraft.value[f.key] = ''
    }
    await store.refresh(true)
  } catch (value) {
    if (current(version)) showError(value)
  } finally {
    if (current(version)) saving.value = false
  }
}

async function test(): Promise<void> {
  if (testing.value || !isAdmin.value) return
  const version = generation
  testing.value = true
  error.value = ''
  try {
    const result = await testConnector(props.connectorKey)
    if (!current(version)) return
    testResult.value = result
    const [updated] = await Promise.all([getConnector(props.connectorKey), store.refresh(true)])
    if (!current(version)) return
    detail.value = updated
  } catch (value) {
    if (current(version)) showError(value)
  } finally {
    if (current(version)) testing.value = false
  }
}

async function toggleEnabled(): Promise<void> {
  if (!detail.value || !isAdmin.value || changingEnabled.value) return
  const version = generation
  changingEnabled.value = true
  error.value = ''
  testResult.value = null
  authResult.value = null
  try {
    const updated = await setConnectorEnabled(props.connectorKey, !detail.value.enabled)
    if (!current(version)) return
    detail.value = updated
    const [latest] = await Promise.all([getConnector(props.connectorKey), store.refresh(true)])
    if (current(version)) detail.value = latest
  } catch (value) {
    if (current(version)) showError(value)
  } finally {
    if (current(version)) changingEnabled.value = false
  }
}

async function acceptAuth(result: AuthResult, version: number): Promise<void> {
  if (!current(version)) return
  error.value = ''
  authResult.value = result
  if (result.state === 'completed') {
    authenticating.value = false
    await test()
    return
  }
  if (result.state === 'expired' || result.state === 'failed') {
    authenticating.value = false
    return
  }
  if (Date.now() >= expiresAt) {
    authResult.value = { ...result, state: 'expired' }
    authenticating.value = false
    return
  }
  // 完成本次请求后才安排下一次，防止慢请求与旧空间响应交叉。
  pollTimer = setTimeout(() => { void pollAuth(version) }, Math.min(AUTH_POLL_MS, expiresAt - Date.now()))
}

async function pollAuth(version: number): Promise<void> {
  if (!current(version)) return
  if (Date.now() >= expiresAt && authResult.value) {
    await acceptAuth({ ...authResult.value, state: 'expired' }, version)
    return
  }
  try {
    await acceptAuth(await getConnectorAuthStatus(props.connectorKey), version)
  } catch (value) {
    if (!current(version)) return
    showError(value)
    if (isYangJiBao.value && value instanceof ApiError && [401, 403, 404].includes(value.status)) {
      authResult.value = null
      authenticating.value = false
      return
    }
    pollTimer = setTimeout(() => { void pollAuth(version) }, Math.max(0, Math.min(AUTH_POLL_MS, expiresAt - Date.now())))
  }
}

async function authenticate(): Promise<void> {
  if (authenticating.value || !isAdmin.value) return
  clearTimeout(pollTimer)
  const version = generation
  authenticating.value = true
  error.value = ''
  authResult.value = null
  testResult.value = null
  try {
    const result = await authenticateConnector(props.connectorKey)
    if (!current(version)) return
    const serverExpiry = Date.parse(result.expires_at ?? '')
    expiresAt = Number.isFinite(serverExpiry) ? serverExpiry : Date.now() + AUTH_TIMEOUT_MS
    await acceptAuth(result, version)
  } catch (value) {
    if (current(version)) { showError(value); authenticating.value = false }
  }
}

function stop(): void {
  generation++
  clearTimeout(pollTimer)
  authResult.value = null
  authenticating.value = false
}

function close(): void {
  stopped = true
  stop()
  emit('close')
}

watch(() => [store.scope, props.connectorKey], () => {
  stop()
  detail.value = null
  testResult.value = null
  configDraft.value = {}
  error.value = ''
  saving.value = false
  testing.value = false
  changingEnabled.value = false
  if (store.scope) void load()
}, { flush: 'sync' })
onMounted(load)
onUnmounted(() => { stopped = true; stop() })
</script>

<template>
  <div
    class="connector-dialog__overlay"
    @click.self="close"
  >
    <div class="connector-dialog">
      <header class="connector-dialog__header">
        <h2>{{ detail?.name ?? '加载中…' }}</h2>
        <button
          class="connector-dialog__close"
          @click="close"
        >
          ✕
        </button>
      </header>

      <div
        v-if="loading"
        class="connector-dialog__loading"
      >
        加载中…
      </div>

      <template v-else-if="detail">
        <!-- 状态信息 -->
        <div class="connector-dialog__status">
          <span>{{ detail.kind.toUpperCase() }}</span>
          <span>工具数：{{ detail.tools.length }}</span>
          <span>{{ connectorHealthLabel(detail) }}</span>
          <span
            v-if="detail.last_error"
            class="connector-dialog__status-error"
          >
            {{ detail.last_error }}
          </span>
        </div>
        <div class="connector-dialog__section">
          <p>最近检查 {{ connectorTime(detail.last_checked_at) }}</p>
          <p>最近成功 {{ connectorTime(detail.last_success_at) }}</p>
          <button
            v-if="isAdmin"
            type="button"
            class="connector-dialog__btn connector-dialog__btn--secondary"
            :disabled="changingEnabled || authenticating || testing || saving"
            @click="toggleEnabled"
          >
            {{ changingEnabled ? '正在更新…' : detail.enabled ? '停用连接器' : '启用连接器' }}
          </button>
          <p v-if="!isAdmin">
            请联系管理员配置或授权连接器
          </p>
        </div>

        <YangJiBaoHoldings
          v-if="isYangJiBao && detail.enabled && detail.can_view_holdings && !authenticating"
          :key="store.scope"
        />

        <!-- 配置表单 -->
        <section
          v-if="detail.config_fields.length"
          class="connector-dialog__section"
        >
          <h3>配置</h3>
          <div
            v-for="field in detail.config_fields"
            :key="field.key"
            class="connector-dialog__field"
          >
            <label class="connector-dialog__label">
              {{ field.label }}
              <span
                v-if="field.required"
                class="connector-dialog__required"
              >*</span>
            </label>
            <input
              v-if="field.is_secret"
              v-model="configDraft[field.key]"
              type="password"
              class="connector-dialog__input"
              :placeholder="detail.config_values[field.key] ? '已配置，留空保留原密钥' : '输入新的密钥'"
              autocomplete="new-password"
              :disabled="!isAdmin"
            />
            <input
              v-else-if="field.field_type === 'number'"
              v-model="configDraft[field.key]"
              type="number"
              class="connector-dialog__input"
              :placeholder="field.placeholder"
              :disabled="!isAdmin"
            />
            <input
              v-else
              v-model="configDraft[field.key]"
              type="text"
              class="connector-dialog__input"
              :placeholder="field.placeholder || field.default"
              :disabled="!isAdmin"
            />
          </div>
        </section>

        <!-- 工具列表 -->
        <section
          v-if="detail.tools.length"
          class="connector-dialog__section"
        >
          <h3>工具（{{ detail.tools.length }}）</h3>
          <div
            v-for="tool in detail.tools"
            :key="tool.name"
            class="connector-dialog__tool"
          >
            <div class="connector-dialog__tool-header">
              <strong>{{ tool.name }}</strong>
              <span class="connector-dialog__risk">{{ tool.risk }}</span>
            </div>
            <p class="connector-dialog__tool-desc">
              {{ tool.description }}
            </p>
          </div>
        </section>

        <!-- 测试结果 -->
        <div
          v-if="testResult"
          class="connector-dialog__test-result"
        >
          <span :class="testResult.status === 'connected' ? 'connector-dialog__test-ok' : 'connector-dialog__test-fail'">
            {{ testResult.status === 'connected' ? '✓ 连接正常' : '✗ 连接失败' }}
          </span>
          <span>{{ testResult.message }}（发现 {{ testResult.tool_count }} 个工具）</span>
        </div>

        <p
          v-if="error"
          class="connector-dialog__error"
        >
          {{ error }}
        </p>

        <!-- 授权结果 -->
        <div
          v-if="authResult"
          class="connector-dialog__auth-result"
          aria-live="polite"
        >
          <p>{{ qrContent ? '等待微信扫码' : authLabels[authResult.state] }}</p>
          <p v-if="authResult.expires_at">
            授权截止 {{ connectorTime(authResult.expires_at) }}
          </p>
          <template v-if="qrContent">
            <p>{{ authResult.output }}</p>
            <img
              v-if="qrImage"
              :src="qrImage"
              :width="QR_WIDTH_PX"
              :height="QR_WIDTH_PX"
              class="connector-dialog__qr"
              alt="养基宝微信扫码授权二维码"
            />
            <p v-else>
              {{ qrError || '正在生成二维码…' }}
            </p>
          </template>
          <p
            v-else-if="authResult.already_logged_in"
            class="connector-dialog__auth-ok"
          >
            ✓ {{ authResult.output }}
          </p>
          <p v-else-if="trustedAuthUrl && authenticating">
            请在浏览器中完成授权：
            <a
              :href="trustedAuthUrl"
              target="_blank"
              rel="noopener noreferrer"
            >打开授权页面（{{ authHost }}）</a>
          </p>
          <p
            v-else
            class="connector-dialog__auth-output"
          >
            {{ authResult.output }}
          </p>
        </div>

        <!-- 操作按钮 -->
        <footer
          v-if="isAdmin"
          class="connector-dialog__footer"
        >
          <button
            v-if="canStartAuth"
            class="connector-dialog__btn connector-dialog__btn--secondary"
            :disabled="authenticating || testing || saving || changingEnabled"
            @click="authenticate"
          >
            {{ authButtonLabel }}
          </button>
          <button
            class="connector-dialog__btn connector-dialog__btn--secondary"
            :disabled="testing || authenticating || saving || changingEnabled || !detail.enabled"
            @click="test"
          >
            {{ testing ? '检查中…' : '重新检查' }}
          </button>
          <button
            v-if="detail.config_fields.length"
            class="connector-dialog__btn connector-dialog__btn--primary"
            :disabled="saving || authenticating || testing || changingEnabled"
            @click="save"
          >
            {{ saving ? '保存中…' : '保存配置' }}
          </button>
        </footer>
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
.connector-dialog__overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  justify-content: flex-end;
  z-index: 100;
}

.connector-dialog {
  width: 480px;
  max-width: 100vw;
  height: 100vh;
  background: #fff;
  box-shadow: -4px 0 20px rgba(0, 0, 0, 0.15);
  display: flex;
  flex-direction: column;
  overflow-y: auto;

  &__header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.25rem 1.5rem;
    border-bottom: 1px solid #eee;

    h2 {
      margin: 0;
      font-size: 1.15rem;
    }
  }

  &__close {
    background: none;
    border: none;
    font-size: 1.25rem;
    cursor: pointer;
    color: #888;
    padding: 0.25rem;

    &:hover { color: #333; }
  }

  &__loading {
    padding: 2rem;
    color: #888;
  }

  &__status {
    display: flex;
    gap: 1rem;
    padding: 0.75rem 1.5rem;
    font-size: 0.85rem;
    color: #666;
    border-bottom: 1px solid #f0f0f0;
  }

  &__status-error {
    color: #c0392b;
  }

  &__section {
    padding: 1rem 1.5rem;

    h3 {
      margin: 0 0 0.75rem;
      font-size: 0.95rem;
      font-weight: 600;
    }
  }

  &__field {
    margin-bottom: 1rem;
  }

  &__label {
    display: block;
    font-size: 0.85rem;
    font-weight: 500;
    margin-bottom: 0.35rem;
    color: #333;
  }

  &__required {
    color: #e74c3c;
  }

  &__input {
    width: 100%;
    padding: 0.55rem 0.75rem;
    border: 1px solid #ddd;
    border-radius: 6px;
    font-size: 0.9rem;
    box-sizing: border-box;

    &:focus {
      outline: none;
      border-color: #3498db;
      box-shadow: 0 0 0 2px rgba(52, 152, 219, 0.15);
    }
  }

  &__tool {
    padding: 0.65rem 0;
    border-bottom: 1px solid #f5f5f5;

    &:last-child { border-bottom: none; }
  }

  &__tool-header {
    display: flex;
    justify-content: space-between;
    align-items: center;

    strong {
      font-size: 0.9rem;
    }
  }

  &__risk {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    background: #f0f0f0;
    color: #555;
  }

  &__tool-desc {
    margin: 0.25rem 0 0;
    font-size: 0.82rem;
    color: #666;
  }

  &__test-result {
    margin: 0.75rem 1.5rem;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    background: #f8f9fa;
    font-size: 0.85rem;
    display: flex;
    gap: 0.75rem;
    align-items: center;
  }

  &__auth-result {
    margin: 0.75rem 1.5rem;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    background: #eaf4ff;
    font-size: 0.85rem;

    a {
      color: #3498db;
      font-weight: 600;
    }
  }

  &__auth-output {
    margin: 0;
    color: #555;
    word-break: break-all;
  }

  &__qr {
    display: block;
    max-width: 100%;
    height: auto;
    margin: 1rem auto 0.25rem;
    border: 1px solid #dce6e0;
    border-radius: 8px;
    background: #fff;
  }

  &__auth-ok {
    margin: 0;
    color: #27ae60;
    font-weight: 600;
  }

  &__test-ok {
    color: #27ae60;
    font-weight: 600;
  }

  &__test-fail {
    color: #e74c3c;
    font-weight: 600;
  }

  &__error {
    margin: 0.5rem 1.5rem;
    color: #c0392b;
    font-size: 0.85rem;
  }

  &__footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    padding: 1.25rem 1.5rem;
    margin-top: auto;
    border-top: 1px solid #eee;
  }

  &__btn {
    padding: 0.55rem 1.25rem;
    border: none;
    border-radius: 8px;
    font-size: 0.9rem;
    cursor: pointer;
    transition: opacity 0.2s;

    &:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    &--primary {
      background: #3498db;
      color: #fff;

      &:hover:not(:disabled) {
        background: #2980b9;
      }
    }

    &--secondary {
      background: #ecf0f1;
      color: #333;

      &:hover:not(:disabled) {
        background: #ddd;
      }
    }
  }
}
</style>
