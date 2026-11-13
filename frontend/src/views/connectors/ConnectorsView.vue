<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAuthStore } from '../../stores/auth'
import {
  listBuiltinConnectors,
  listConnectors,
  installConnector,
  setConnectorEnabled,
  type BuiltinConnector,
  type ConnectorView,
} from '../../api/connectors'
import ConnectorConfigDialog from './ConnectorConfigDialog.vue'

const auth = useAuthStore()
const connectors = ref<ConnectorView[]>([])
const builtins = ref<BuiltinConnector[]>([])
const loading = ref(false)
const error = ref('')
const busy = ref('')
const selectedKey = ref('')
const showConfig = ref(false)

const isAdmin = () => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active'

const statusLabels: Record<string, string> = {
  connected: '已连接',
  degraded: '降级',
  error: '错误',
  disabled: '未启用',
}
const kindLabels: Record<string, string> = { cli: 'CLI', mcp: 'MCP', api: 'API' }

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [installed, builtinList] = await Promise.all([
      listConnectors(),
      listBuiltinConnectors().catch(() => [] as BuiltinConnector[]),
    ])
    connectors.value = installed
    builtins.value = builtinList
  } catch (value) {
    showError(value)
  } finally {
    loading.value = false
  }
}

async function install(builtin: BuiltinConnector): Promise<void> {
  if (busy.value || !isAdmin() || builtin.installed) return
  busy.value = builtin.key
  try {
    await installConnector(builtin.key)
    await load()
  } catch (value) {
    showError(value)
  } finally {
    busy.value = ''
  }
}

function isInstalled(key: string): boolean {
  return connectors.value.some((c) => c.key === key)
}

function openConfig(key: string): void {
  selectedKey.value = key
  showConfig.value = true
}

function onConfigClosed(): void {
  showConfig.value = false
  selectedKey.value = ''
  void load()
}

async function toggleEnabled(connector: ConnectorView): Promise<void> {
  if (busy.value || !isAdmin()) return
  busy.value = connector.key
  try {
    await setConnectorEnabled(connector.key, !connector.enabled)
    await load()
  } catch (value) {
    showError(value)
  } finally {
    busy.value = ''
  }
}

function statusClass(status: string): string {
  return `connectors__status--${status}`
}

onMounted(load)
</script>

<template>
  <section class="connectors">
    <header class="connectors__heading">
      <div>
        <span class="connectors__eyebrow">CONNECTORS</span>
        <h1>连接器</h1>
        <p>集成外部工具与服务（CLI / MCP / API），为 Agent 提供扩展能力。</p>
      </div>
    </header>

    <p
      v-if="error"
      class="connectors__error"
    >
      {{ error }}
    </p>
    <p
      v-if="loading"
      class="connectors__loading"
    >
      加载中…
    </p>

    <div
      v-if="!loading && connectors.length"
      class="connectors__grid"
    >
      <article
        v-for="connector in connectors"
        :key="connector.key"
        class="connectors__card"
        @click="openConfig(connector.key)"
      >
        <div class="connectors__card-header">
          <span class="connectors__icon">{{ connector.icon || '⬡' }}</span>
          <div class="connectors__card-meta">
            <h3 class="connectors__card-name">
              {{ connector.name }}
            </h3>
            <span class="connectors__kind">{{ kindLabels[connector.kind] ?? connector.kind }}</span>
          </div>
          <div class="connectors__card-actions">
            <span :class="['connectors__status', statusClass(connector.status)]">
              {{ statusLabels[connector.status] ?? connector.status }}
            </span>
            <label
              class="connectors__toggle"
              @click.stop
            >
              <input
                type="checkbox"
                :checked="connector.enabled"
                :disabled="!isAdmin() || busy === connector.key"
                @change.stop="toggleEnabled(connector)"
              />
              <span class="connectors__toggle-track" />
            </label>
          </div>
        </div>
        <p class="connectors__card-desc">
          {{ connector.description }}
        </p>
        <div class="connectors__card-footer">
          <span class="connectors__tool-count">{{ connector.tool_count }} 个工具</span>
          <span
            v-if="connector.last_error"
            class="connectors__card-error"
          >{{ connector.last_error }}</span>
        </div>
      </article>
    </div>

    <div
      v-else-if="!loading"
      class="connectors__empty"
    >
      <p>暂无已安装连接器。可从下方目录安装。</p>
    </div>

    <!-- 内置连接器目录 -->
    <section
      v-if="builtins.length"
      class="connectors__section"
    >
      <h2 class="connectors__section-title">
        内置连接器目录
      </h2>
      <div class="connectors__grid">
        <article
          v-for="builtin in builtins"
          :key="builtin.key"
          class="connectors__card connectors__card--builtin"
          @click="isInstalled(builtin.key) ? openConfig(builtin.key) : install(builtin)"
        >
          <div class="connectors__card-header">
            <span class="connectors__icon">{{ builtin.icon || '⬡' }}</span>
            <div class="connectors__card-meta">
              <h3 class="connectors__card-name">
                {{ builtin.name }}
              </h3>
              <span class="connectors__kind">{{ kindLabels[builtin.kind] ?? builtin.kind }}</span>
            </div>
            <div class="connectors__card-actions">
              <span
                v-if="isInstalled(builtin.key)"
                class="connectors__status connectors__status--connected"
              >
                已安装
              </span>
              <button
                v-else-if="isAdmin()"
                class="connectors__install-btn"
                :disabled="busy === builtin.key"
                @click.stop="install(builtin)"
              >
                {{ busy === builtin.key ? '安装中…' : '+ 安装' }}
              </button>
              <span
                v-else
                class="connectors__status connectors__status--disabled"
              >
                未安装
              </span>
            </div>
          </div>
          <p class="connectors__card-desc">
            {{ builtin.description }}
          </p>
          <div class="connectors__card-footer">
            <span class="connectors__tool-count">
              {{ builtin.config_fields.length }} 个配置项
            </span>
          </div>
        </article>
      </div>
    </section>

    <ConnectorConfigDialog
      v-if="showConfig && selectedKey"
      :connector-key="selectedKey"
      @close="onConfigClosed"
    />
  </section>
</template>

<style scoped lang="scss">
.connectors {
  padding: 2rem;
  max-width: 1200px;
  margin: 0 auto;

  &__heading {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 1.5rem;

    h1 {
      margin: 0.25rem 0;
      font-size: 1.75rem;
      font-weight: 700;
    }

    p {
      margin: 0;
      color: #666;
      font-size: 0.95rem;
    }
  }

  &__eyebrow {
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    color: #888;
    text-transform: uppercase;
  }

  &__error {
    color: #c0392b;
    margin-bottom: 1rem;
    font-size: 0.9rem;
  }

  &__loading {
    color: #888;
    margin-bottom: 1rem;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 1.25rem;
  }

  &__card {
    background: #fff;
    border: 1px solid #e0e0e0;
    border-radius: 12px;
    padding: 1.25rem;
    cursor: pointer;
    transition: box-shadow 0.2s, border-color 0.2s;

    &:hover {
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
      border-color: #bbb;
    }
  }

  &__card-header {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
  }

  &__icon {
    font-size: 1.75rem;
    line-height: 1;
  }

  &__card-meta {
    flex: 1;
    min-width: 0;
  }

  &__card-name {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 600;
  }

  &__kind {
    display: inline-block;
    font-size: 0.7rem;
    font-weight: 600;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    background: #f0f0f0;
    color: #555;
    margin-top: 0.25rem;
  }

  &__card-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  &__status {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.75rem;
    color: #888;

    &::before {
      content: '';
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #ccc;
    }

    &--connected::before { background: #27ae60; }
    &--degraded::before { background: #f39c12; }
    &--error::before { background: #e74c3c; }
    &--disabled::before { background: #bbb; }
  }

  &__toggle {
    position: relative;
    display: inline-flex;
    cursor: pointer;

    input {
      position: absolute;
      opacity: 0;
      width: 0;
      height: 0;
    }
  }

  &__toggle-track {
    width: 36px;
    height: 20px;
    border-radius: 10px;
    background: #ddd;
    position: relative;
    transition: background 0.2s;

    &::after {
      content: '';
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: #fff;
      transition: transform 0.2s;
    }

    input:checked + & {
      background: #27ae60;

      &::after {
        transform: translateX(16px);
      }
    }
  }

  &__card-desc {
    font-size: 0.85rem;
    color: #666;
    margin: 0.5rem 0;
    line-height: 1.5;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  &__card-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 0.5rem;
  }

  &__tool-count {
    font-size: 0.8rem;
    color: #888;
  }

  &__card-error {
    font-size: 0.75rem;
    color: #c0392b;
    max-width: 180px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__empty {
    text-align: center;
    padding: 3rem;
    color: #888;
  }

  &__section {
    margin-top: 2.5rem;
    padding-top: 2rem;
    border-top: 1px solid #eee;
  }

  &__section-title {
    margin: 0 0 1.25rem;
    font-size: 1.15rem;
    font-weight: 600;
    color: #333;
  }

  &__card--builtin {
    cursor: default;
  }

  &__install-btn {
    padding: 0.4rem 0.9rem;
    border: 1px solid #27ae60;
    background: #fff;
    color: #27ae60;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;

    &:hover:not(:disabled) {
      background: #27ae60;
      color: #fff;
    }

    &:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
  }
}
</style>
