<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  getConnector,
  updateConnectorConfig,
  testConnector,
  authenticateConnector,
  type ConnectorDetail,
  type ConnectorTestResult,
} from '../../api/connectors'

const props = defineProps<{ connectorKey: string }>()
const emit = defineEmits<{ close: [] }>()

const detail = ref<ConnectorDetail | null>(null)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const authenticating = ref(false)
const error = ref('')
const testResult = ref<ConnectorTestResult | null>(null)
const authResult = ref<{ auth_url: string; output: string; already_logged_in: boolean } | null>(null)

const configDraft = ref<Record<string, string>>({})
const secretsDraft = ref<Record<string, string>>({})

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    detail.value = await getConnector(props.connectorKey)
    configDraft.value = { ...detail.value.config_values }
    // 密钥字段初始化为空（留空表示不修改）
    for (const f of detail.value.config_fields) {
      if (f.is_secret) {
        configDraft.value[f.key] = ''
        secretsDraft.value[f.key] = ''
      }
    }
  } catch (value) {
    showError(value)
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (saving.value || !detail.value) return
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
    detail.value = await updateConnectorConfig(props.connectorKey, nonSecret, secrets)
    configDraft.value = { ...detail.value.config_values }
    for (const f of detail.value.config_fields) {
      if (f.is_secret) configDraft.value[f.key] = ''
    }
  } catch (value) {
    showError(value)
  } finally {
    saving.value = false
  }
}

async function test(): Promise<void> {
  if (testing.value) return
  testing.value = true
  error.value = ''
  try {
    testResult.value = await testConnector(props.connectorKey)
    if (testResult.value.status === 'connected') {
      detail.value = await getConnector(props.connectorKey)
    }
  } catch (value) {
    showError(value)
  } finally {
    testing.value = false
  }
}

async function authenticate(): Promise<void> {
  if (authenticating.value) return
  authenticating.value = true
  error.value = ''
  authResult.value = null
  try {
    authResult.value = await authenticateConnector(props.connectorKey)
    if (authResult.value.auth_url) {
      window.open(authResult.value.auth_url, '_blank')
    }
  } catch (value) {
    showError(value)
  } finally {
    authenticating.value = false
  }
}

function close(): void {
  emit('close')
}

onMounted(load)
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
          <span
            v-if="detail.last_error"
            class="connector-dialog__status-error"
          >
            {{ detail.last_error }}
          </span>
        </div>

        <!-- 配置表单 -->
        <section class="connector-dialog__section">
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
              :placeholder="configDraft[field.key] || '输入新的密钥'"
              autocomplete="new-password"
            />
            <input
              v-else-if="field.field_type === 'number'"
              v-model="configDraft[field.key]"
              type="number"
              class="connector-dialog__input"
              :placeholder="field.placeholder"
            />
            <input
              v-else
              v-model="configDraft[field.key]"
              type="text"
              class="connector-dialog__input"
              :placeholder="field.placeholder || field.default"
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
        >
          <p
            v-if="authResult.already_logged_in"
            class="connector-dialog__auth-ok"
          >
            ✓ {{ authResult.output }}
          </p>
          <p v-else-if="authResult.auth_url">
            请在浏览器中完成授权：
            <a
              :href="authResult.auth_url"
              target="_blank"
            >打开授权页面</a>
          </p>
          <p
            v-else
            class="connector-dialog__auth-output"
          >
            {{ authResult.output }}
          </p>
        </div>

        <!-- 操作按钮 -->
        <footer class="connector-dialog__footer">
          <button
            class="connector-dialog__btn connector-dialog__btn--secondary"
            :disabled="authenticating"
            @click="authenticate"
          >
            {{ authenticating ? '授权中…' : '账号授权' }}
          </button>
          <button
            class="connector-dialog__btn connector-dialog__btn--secondary"
            :disabled="testing"
            @click="test"
          >
            {{ testing ? '测试中…' : '测试连接' }}
          </button>
          <button
            class="connector-dialog__btn connector-dialog__btn--primary"
            :disabled="saving"
            @click="save"
          >
            {{ saving ? '保存中…' : '保存' }}
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
