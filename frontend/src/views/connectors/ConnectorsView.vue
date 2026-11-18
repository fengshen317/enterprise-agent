<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useConnectorsStore } from '../../stores/connectors'
import {
  listBuiltinConnectors,
  connectorHealthLabel,
  installConnector,
  setConnectorEnabled,
  type BuiltinConnector,
  type ConnectorHealth,
  type ConnectorView,
} from '../../api/connectors'
import ConnectorConfigDialog from './ConnectorConfigDialog.vue'

interface ConnectorCard {
  key: string
  name: string
  description: string
  icon: string
  installed: boolean
  connection?: ConnectorView
}

const auth = useAuthStore()
const store = useConnectorsStore()
const route = useRoute()
const router = useRouter()
const builtins = ref<BuiltinConnector[]>([])
const search = ref('')
const catalogLoading = ref(true)
const catalogError = ref('')
const operationError = ref('')
const busy = ref('')
const selectedKey = ref('')
const isAdmin = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
let active = true

const visibleCards = computed(() => {
  const cards = new Map<string, ConnectorCard>()
  for (const builtin of builtins.value) cards.set(builtin.key, builtin)
  for (const connection of store.connectors) {
    cards.set(connection.key, { ...connection, installed: true, connection })
  }
  const query = search.value.trim().toLocaleLowerCase()
  return [...cards.values()].filter(card =>
    [card.name, card.description, card.key].some(text => text.toLocaleLowerCase().includes(query)),
  )
})

function needsConnection(connector: ConnectorHealth & { enabled: boolean }): boolean {
  return !connector.enabled || connector.auth_status === 'required'
    || connector.auth_action === 'authorize' || connector.auth_action === 'configure'
}

function addLabel(card: ConnectorCard): string {
  if (!isAdmin.value) return `请联系管理员连接${card.name}`
  if (!card.installed) return `添加${card.name}`
  return card.connection?.enabled ? `连接${card.name}` : `启用${card.name}`
}

function statusClass(connector: ConnectorView): string {
  const status = connectorHealthLabel(connector)
  if (status === '健康') return 'connectors__status--healthy'
  return status === '连接异常' ? 'connectors__status--error' : 'connectors__status--pending'
}

async function loadCatalog(): Promise<void> {
  catalogLoading.value = true
  catalogError.value = ''
  try {
    builtins.value = await listBuiltinConnectors()
  } catch (value) {
    catalogError.value = value instanceof Error ? value.message : '连接器目录加载失败'
  } finally {
    catalogLoading.value = false
  }
}

async function retry(): Promise<void> {
  await Promise.all([loadCatalog(), store.refresh(true)])
}

async function connect(card: ConnectorCard): Promise<void> {
  if (!isAdmin.value || !store.scope || busy.value) return
  if (card.connection?.enabled) {
    selectedKey.value = card.key
    return
  }
  const scope = store.scope
  const current = () => active && scope === store.scope
  busy.value = card.key
  operationError.value = ''
  try {
    if (!card.installed) await installConnector(card.key)
    // 安装是独立操作，切换工作空间后由新页面中的用户继续启用。
    if (!current()) return
    const connector = await setConnectorEnabled(card.key, true)
    if (current() && needsConnection(connector)) selectedKey.value = card.key
  } catch (value) {
    if (current()) operationError.value = value instanceof Error ? value.message : '连接失败，请重试'
  } finally {
    if (current()) {
      await store.refresh(true)
      busy.value = ''
    }
  }
}

function closeConfig(): void {
  selectedKey.value = ''
  if (route.query.connector) void router.replace({ query: { ...route.query, connector: undefined } })
}

watch(() => route.query.connector, key => {
  if (typeof key === 'string' && key) selectedKey.value = key
}, { immediate: true })
onMounted(() => {
  store.acquire()
  void loadCatalog()
})
onUnmounted(() => {
  active = false
  store.release()
})
</script>

<template>
  <section class="connectors">
    <header class="connectors__heading">
      <h1>连接器</h1>
      <input
        v-model="search"
        class="connectors__search"
        type="search"
        aria-label="搜索连接器"
        placeholder="搜索连接器"
      />
    </header>

    <div
      v-if="operationError || catalogError || store.error"
      class="connectors__feedback"
    >
      <p role="alert">
        {{ operationError || catalogError || store.error }}
      </p>
      <button
        v-if="catalogError || store.error"
        type="button"
        class="connectors__text-button"
        @click="retry"
      >
        重新加载
      </button>
    </div>

    <p
      v-if="catalogLoading || store.loading"
      class="connectors__empty"
      role="status"
    >
      正在加载连接器…
    </p>
    <div
      v-else-if="visibleCards.length"
      class="connectors__grid"
    >
      <article
        v-for="card in visibleCards"
        :key="card.key"
        class="connectors__card"
        :data-connector="card.key"
      >
        <span
          class="connectors__icon"
          aria-hidden="true"
        >{{ card.icon || '⬡' }}</span>
        <div class="connectors__card-body">
          <h2 class="connectors__card-name">
            <button
              v-if="card.connection"
              type="button"
              class="connectors__text-button"
              @click="selectedKey = card.key"
            >
              {{ card.name }}
            </button>
            <span v-else>{{ card.name }}</span>
          </h2>
          <p
            class="connectors__card-desc"
            :title="card.description"
          >
            {{ card.description }}
          </p>
        </div>
        <div class="connectors__actions">
          <template v-if="card.connection && !needsConnection(card.connection)">
            <span
              :class="['connectors__status', statusClass(card.connection)]"
              role="img"
              :aria-label="connectorHealthLabel(card.connection)"
              :title="connectorHealthLabel(card.connection)"
            />
            <button
              type="button"
              class="connectors__action connectors__action--manage"
              :aria-label="`管理${card.name}`"
              :title="`管理${card.name}`"
              @click="selectedKey = card.key"
            >
              <span aria-hidden="true">···</span>
            </button>
          </template>
          <button
            v-else
            type="button"
            class="connectors__action connectors__action--add"
            :disabled="!isAdmin || !store.scope || !!busy || !!store.error"
            :aria-label="busy === card.key ? `正在连接${card.name}` : addLabel(card)"
            :title="addLabel(card)"
            @click="connect(card)"
          >
            <span aria-hidden="true">{{ busy === card.key ? '…' : '+' }}</span>
          </button>
        </div>
      </article>
    </div>
    <p
      v-else-if="!catalogError && !store.error"
      class="connectors__empty"
      role="status"
    >
      {{ search.trim() ? '没有找到匹配的连接器' : '暂无可用连接器' }}
    </p>

    <ConnectorConfigDialog
      v-if="selectedKey"
      :key="selectedKey"
      :connector-key="selectedKey"
      @close="closeConfig"
    />
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.connectors {
  max-width: 1320px;
  margin: 0 auto;
  padding: clamp(1.25rem, 3vw, 2.5rem);
  color: $ink;

  &__heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1.5rem;

    h1 { margin: 0; font-size: 1.65rem; line-height: 1.25; }
  }

  &__search {
    width: min(100%, 16rem);
    min-height: 2.5rem;
    padding: 0.55rem 0.8rem;
    border: 1px solid $line;
    border-radius: 8px;
    background: #fff;
    color: inherit;
    font: inherit;

    &::placeholder { color: #56665c; }
  }

  &__feedback {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin-bottom: 1rem;
    color: #963c2d;
    font-size: 0.875rem;

    p { margin: 0; }
    button { text-decoration: underline; }
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 315px), 1fr));
    gap: 0.9rem;
  }

  &__card {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    min-width: 0;
    padding: 1rem;
    border: 1px solid $line;
    border-radius: 12px;
    background: #fff;
    transition: border-color 160ms ease, background-color 160ms ease;

    &:hover { border-color: #b2c7b8; background: #fcfefc; }
  }

  &__icon {
    display: grid;
    flex: 0 0 2.75rem;
    height: 2.75rem;
    place-items: center;
    border-radius: 10px;
    background: $paper;
    font-size: 1.35rem;
    line-height: 1;
  }

  &__card-body { flex: 1; min-width: 0; }
  &__card-name { margin: 0; font-size: 1rem; line-height: 1.35; overflow-wrap: anywhere; }
  &__card-desc {
    display: -webkit-box;
    overflow: hidden;
    margin: 0.25rem 0 0;
    color: #56665c;
    font-size: 0.8125rem;
    line-height: 1.45;
    overflow-wrap: anywhere;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  &__actions {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 0.45rem;
  }

  &__status {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;

    &--healthy { background: $green; }
    &--pending { background: #aa660d; }
    &--error { background: #a74336; }
  }

  &__text-button {
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: inherit;
    cursor: pointer;

    &:hover { text-decoration: underline; text-underline-offset: 0.15em; }
  }

  &__action {
    display: grid;
    width: 2.25rem;
    height: 2.25rem;
    padding: 0;
    place-items: center;
    border: 0;
    border-radius: 8px;
    color: $ink;
    line-height: 1;
    cursor: pointer;

    &--add { background: #e4efe6; font-size: 1.45rem; }
    &--manage { background: transparent; font-size: 1.35rem; font-weight: 700; }
    &:hover:not(:disabled) { background: #d9ebde; }
    &:disabled { opacity: 0.5; cursor: not-allowed; }
  }

  &__search:focus-visible,
  &__text-button:focus-visible,
  &__action:focus-visible { outline: 2px solid $green; outline-offset: 2px; }

  &__empty { margin: 0; padding: 2rem 0; color: #56665c; }
}

@media (prefers-reduced-motion: reduce) {
  .connectors__card { transition: none; }
}
</style>
