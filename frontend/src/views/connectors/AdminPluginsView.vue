<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../../api/client'
import {
  connectorHealthLabel,
  installConnector,
  isPersonalConnector,
  listBuiltinConnectors,
  setConnectorEnabled,
  type BuiltinConnector,
  type ConnectorView,
} from '../../api/connectors'
import { useAuthStore } from '../../stores/auth'
import { useConnectorsStore } from '../../stores/connectors'
import ConnectorConfigDialog from './ConnectorConfigDialog.vue'

type Plugin = Pick<BuiltinConnector, 'key' | 'name' | 'description' | 'icon' | 'installed'> & {
  connection?: ConnectorView
}
type Selection = { key: string; mode: 'detail' } | { key: string; mode: 'change'; enabled: boolean }
type StatusFilter = 'all' | 'enabled' | 'disabled'

defineProps<{ embedded?: boolean }>()

const auth = useAuthStore()
const store = useConnectorsStore()
const route = useRoute()
const router = useRouter()
const scope = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active'
  ? store.scope : '')
const builtins = ref<BuiltinConnector[]>([])
const confirmedInstallations = ref(new Set<string>())
const catalogLoading = ref(false)
const catalogError = ref('')
const operationError = ref('')
const feedback = ref('')
const busy = ref(false)
const query = ref('')
const statusFilter = ref<StatusFilter>('all')
const selection = ref<Selection | null>(null)
const managedKey = ref('')
const dialog = ref<HTMLDialogElement | null>(null)
const filterControl = ref<HTMLSelectElement | null>(null)
let opener: HTMLElement | null = null
let generation = 0
let acquired = false

const plugins = computed(() => {
  const items = new Map<string, Plugin>()
  for (const builtin of builtins.value) {
    items.set(builtin.key, { ...builtin, installed: builtin.installed || confirmedInstallations.value.has(builtin.key) })
  }
  for (const connection of store.connectors) items.set(connection.key, { ...connection, installed: true, connection })
  return items
})
const filtered = computed(() => {
  const search = query.value.trim().toLocaleLowerCase()
  return [...plugins.value.values()].filter(item => {
    const enabled = enabledState(item)
    return [item.name, item.description, item.key].some(value => value.toLocaleLowerCase().includes(search))
      && (statusFilter.value === 'all' || enabled === (statusFilter.value === 'enabled'))
  })
})
const selectedPlugin = computed(() => plugins.value.get(selection.value?.key ?? ''))
const requestedKey = computed(() => typeof route.query.connector === 'string' ? route.query.connector : '')
const requestedInstalled = computed(() => plugins.value.get(requestedKey.value)?.installed === true)
const error = computed(() => catalogError.value || store.error)
const loading = computed(() => catalogLoading.value || store.loading)
const changeVerb = computed(() => selection.value?.mode === 'change' && selection.value.enabled ? '启用' : '停用')
const confirmation = computed(() => {
  if (selection.value?.mode !== 'change') return ''
  const personal = isPersonalConnector(selection.value.key)
  if (selection.value.enabled) {
    return personal
      ? '启用后，各工作空间的成员可在插件中心绑定自己的账号。'
      : '启用后，插件的启用状态将应用于全部工作空间。'
  }
  return personal
    ? '停用后，所有工作空间将暂停使用此插件。成员已保存的账号绑定会保留。'
    : '停用后，所有工作空间将暂停使用此插件。'
})

function authLabel(plugin: Plugin): string {
  if (isPersonalConnector(plugin.key)) return '成员自行绑定'
  if (plugin.key === 'fund_market' || plugin.connection?.auth_status === 'not_required') return '无需授权'
  if (plugin.key === 'tmeet' || plugin.key === 'yangjibao' || plugin.connection?.auth_action === 'authorize') return '管理员授权'
  if (plugin.connection?.auth_action === 'configure') return '管理员配置'
  return '管理员配置或授权'
}

function enabledState(plugin: Plugin): boolean | undefined {
  if (plugin.connection) return plugin.connection.enabled
  if (!store.error && !store.loading) return false
}

function stateLabel(plugin: Plugin): string {
  const enabled = enabledState(plugin)
  if (enabled === undefined) return '待确认'
  return enabled ? '已启用' : '未启用'
}

function current(version: number): boolean {
  return version === generation && Boolean(scope.value)
}

async function loadCatalog(): Promise<void> {
  if (!scope.value || catalogLoading.value) return
  const version = generation
  catalogLoading.value = true
  catalogError.value = ''
  try {
    const result = await listBuiltinConnectors()
    if (current(version)) builtins.value = result
  } catch (value) {
    if (current(version)) catalogError.value = value instanceof Error ? value.message : '插件目录加载失败'
  } finally {
    if (current(version)) catalogLoading.value = false
  }
}

async function retry(): Promise<void> {
  if (!scope.value || busy.value || loading.value) return
  await Promise.all([loadCatalog(), store.refresh(true)])
}

function openDialog(next: Selection, event: MouseEvent): void {
  if (!dialog.value || !scope.value || busy.value || selection.value) return
  opener = event.currentTarget as HTMLElement
  operationError.value = ''
  selection.value = next
  void nextTick(() => {
    if (dialog.value && scope.value && selection.value && !dialog.value.open) dialog.value.showModal()
  })
}

function closeDialog(restoreFocus = true): void {
  const previous = opener
  opener = null
  if (dialog.value?.open) dialog.value.close()
  selection.value = null
  operationError.value = ''
  if (!restoreFocus) return
  void nextTick(() => {
    if (!scope.value || selection.value || managedKey.value) return
    const focusTarget = previous?.isConnected && !previous.matches(':disabled') ? previous : filterControl.value
    focusTarget?.focus({ preventScroll: true })
  })
}

function dismissDialog(): void {
  if (!busy.value) closeDialog()
}

function openConfig(key: string): void {
  if (!scope.value || busy.value || !plugins.value.get(key)?.installed) return
  const previous = opener
  closeDialog(false)
  opener = previous
  managedKey.value = key
}

function closeConfig(): void {
  managedKey.value = ''
  if (requestedKey.value) void router.replace({ query: { ...route.query, connector: undefined } })
  closeDialog()
}

async function applyChange(): Promise<void> {
  const target = selection.value
  const plugin = selectedPlugin.value
  if (target?.mode !== 'change' || !plugin || !scope.value || busy.value || loading.value || error.value) return

  const version = generation
  let needsInstall = target.enabled && !plugin.installed
  let updated = false
  busy.value = true
  operationError.value = ''
  feedback.value = ''
  try {
    if (needsInstall) {
      try {
        await installConnector(plugin.key)
      } catch (value) {
        if (!(value instanceof ApiError && value.status === 409)) throw value
      }
      // 安装已写入数据库时保留结果，启用失败后只重试启用。
      if (!current(version)) return
      confirmedInstallations.value.add(plugin.key)
      needsInstall = false
    }
    if (Boolean(plugins.value.get(plugin.key)?.connection?.enabled) !== target.enabled) {
      const result = await setConnectorEnabled(plugin.key, target.enabled)
      updated = result.enabled === target.enabled
    } else {
      updated = true
    }
  } catch (value) {
    if (current(version)) operationError.value = value instanceof Error ? value.message : '插件状态更新失败，请重试'
  } finally {
    if (current(version)) {
      // 超时后读取服务端状态，避免重复安装或重复启停。
      await Promise.all([store.refresh(true), needsInstall ? loadCatalog() : Promise.resolve()])
      if (current(version)) {
        busy.value = false
        const latest = plugins.value.get(plugin.key)?.connection
        if (updated || latest?.enabled === target.enabled) {
          feedback.value = `${plugin.name} ${target.enabled ? '已启用' : '已停用'}`
          closeDialog()
        } else if (!operationError.value) {
          operationError.value = '未确认插件状态，请重试'
        }
      }
    }
  }
}

watch(scope, value => {
  generation++
  closeDialog(false)
  managedKey.value = ''
  builtins.value = []
  confirmedInstallations.value.clear()
  catalogLoading.value = false
  catalogError.value = ''
  feedback.value = ''
  busy.value = false
  if (value) {
    if (!acquired) store.acquire()
    acquired = true
    void loadCatalog()
  } else if (acquired) {
    store.release()
    acquired = false
  }
}, { immediate: true, flush: 'sync' })

watch([scope, requestedKey, requestedInstalled], ([activeScope, key, installed]) => {
  if (activeScope && key && installed && !selection.value && !busy.value) openConfig(key)
}, { immediate: true })

onBeforeUnmount(() => {
  generation++
  closeDialog(false)
  if (acquired) store.release()
})
</script>

<template>
  <section
    class="admin-plugins"
    :class="{ 'admin-plugins--embedded': embedded }"
    aria-label="插件管理"
  >
    <header
      v-if="!embedded"
      class="admin-plugins__title"
    >
      <h1>
        插件管理
      </h1>
      <p v-if="scope">
        面向成员启用插件，管理应用的授权方式和启停状态。
      </p>
    </header>
    <p
      v-if="!scope"
      class="admin-plugins__error"
      role="alert"
    >
      仅当前工作空间的管理员可访问此页面。
    </p>
    <template v-else>
      <div class="admin-plugins__scope">
        <span
          class="admin-plugins__scope-icon"
          aria-hidden="true"
        >▱</span>
        <div>
          <strong>启停范围为全部工作空间</strong>
          <p>启用个人插件后，成员在自己的工作空间绑定账号。</p>
        </div>
      </div>

      <section aria-labelledby="admin-plugin-list">
        <div class="admin-plugins__toolbar">
          <h2 id="admin-plugin-list">
            插件列表
          </h2>
          <div class="admin-plugins__filters">
            <input
              v-model="query"
              type="search"
              placeholder="搜索插件"
              aria-label="搜索插件"
            />
            <select
              ref="filterControl"
              v-model="statusFilter"
              aria-label="筛选启停状态"
            >
              <option value="all">
                全部状态
              </option>
              <option value="enabled">
                已启用
              </option>
              <option value="disabled">
                未启用
              </option>
            </select>
          </div>
        </div>
        <div
          v-if="error"
          class="admin-plugins__feedback"
        >
          <p
            class="admin-plugins__error"
            role="alert"
          >
            {{ error }}
          </p>
          <button
            class="admin-plugins__button"
            type="button"
            :disabled="loading || busy"
            @click="retry"
          >
            重新加载
          </button>
        </div>
        <div
          class="admin-plugins__table"
          role="table"
          aria-label="插件列表"
        >
          <div
            class="admin-plugins__columns"
            role="row"
          >
            <div role="columnheader">
              插件
            </div>
            <div role="columnheader">
              授权方式
            </div>
            <div role="columnheader">
              状态
            </div>
            <div role="columnheader">
              操作
            </div>
          </div>
          <div role="rowgroup">
            <div
              v-for="plugin in filtered"
              :key="plugin.key"
              class="admin-plugins__row"
              :data-plugin="plugin.key"
              role="row"
            >
              <div
                class="admin-plugins__product"
                role="cell"
              >
                <span
                  class="admin-plugins__icon"
                  :data-icon="plugin.key"
                  aria-hidden="true"
                >{{ plugin.icon || '⬡' }}</span>
                <div>
                  <strong>{{ plugin.name }}</strong>
                  <p>{{ plugin.description }}</p>
                </div>
              </div>
              <div
                class="admin-plugins__auth"
                role="cell"
              >
                {{ authLabel(plugin) }}
              </div>
              <div
                class="admin-plugins__state"
                role="cell"
              >
                <span :class="{ 'admin-plugins__badge--enabled': plugin.connection?.enabled }">
                  {{ stateLabel(plugin) }}
                </span>
              </div>
              <div
                class="admin-plugins__actions"
                role="cell"
              >
                <button
                  class="admin-plugins__button"
                  type="button"
                  :disabled="busy || loading || !!error"
                  :aria-label="`${plugin.connection?.enabled ? '停用' : '启用'} ${plugin.name}`"
                  @click="openDialog({ key: plugin.key, mode: 'change', enabled: !plugin.connection?.enabled }, $event)"
                >
                  {{ plugin.connection?.enabled ? '停用' : '启用' }}
                </button>
                <button
                  class="admin-plugins__button admin-plugins__button--quiet"
                  type="button"
                  :disabled="busy || loading"
                  :aria-label="`查看 ${plugin.name} 详情`"
                  @click="openDialog({ key: plugin.key, mode: 'detail' }, $event)"
                >
                  详情
                </button>
              </div>
            </div>
          </div>
          <div
            v-if="!filtered.length && !error"
            role="row"
          >
            <div
              class="admin-plugins__empty"
              role="cell"
              aria-colspan="4"
            >
              <p role="status">
                {{ loading ? '正在加载插件…' : query.trim() || statusFilter !== 'all' ? '没有匹配的插件' : '暂无插件' }}
              </p>
            </div>
          </div>
        </div>
        <p
          class="admin-plugins__notice"
          role="status"
        >
          {{ feedback }}
        </p>
      </section>

      <dialog
        ref="dialog"
        class="admin-plugins__dialog"
        aria-labelledby="admin-plugin-dialog-title"
        @cancel.prevent="dismissDialog"
      >
        <template v-if="selection && selectedPlugin">
          <header class="admin-plugins__dialog-heading">
            <div>
              <h2 id="admin-plugin-dialog-title">
                {{ selection.mode === 'change' ? `${changeVerb} ${selectedPlugin.name}` : selectedPlugin.name }}
              </h2>
              <p>{{ selection.mode === 'change' ? '适用于全部工作空间' : '插件详情' }}</p>
            </div>
            <button
              class="admin-plugins__button admin-plugins__button--quiet"
              type="button"
              aria-label="关闭弹窗"
              :disabled="busy"
              @click="dismissDialog"
            >
              ×
            </button>
          </header>
          <template v-if="selection.mode === 'detail'">
            <dl class="admin-plugins__detail">
              <div>
                <dt>插件状态</dt>
                <dd>{{ stateLabel(selectedPlugin) }}</dd>
              </div>
              <div>
                <dt>授权方式</dt>
                <dd>{{ authLabel(selectedPlugin) }}</dd>
              </div>
              <div>
                <dt>启停范围</dt>
                <dd>全部工作空间</dd>
              </div>
              <div v-if="selectedPlugin.description">
                <dt>插件用途</dt>
                <dd>{{ selectedPlugin.description }}</dd>
              </div>
              <div v-if="selectedPlugin.connection?.enabled && !isPersonalConnector(selectedPlugin.key)">
                <dt>连接状态</dt>
                <dd>{{ connectorHealthLabel(selectedPlugin.connection) }}</dd>
              </div>
            </dl>
            <p
              v-if="isPersonalConnector(selectedPlugin.key)"
              class="admin-plugins__detail-note"
            >
              成员在自己的工作空间管理账号，账号凭证仅用于本人运行。
            </p>
          </template>
          <template v-else>
            <p class="admin-plugins__confirmation">
              {{ confirmation }}
            </p>
            <p
              v-if="operationError"
              class="admin-plugins__error"
              role="alert"
            >
              {{ operationError }}
            </p>
            <p
              v-if="error"
              class="admin-plugins__error"
              role="alert"
            >
              {{ error }}
            </p>
          </template>

          <footer class="admin-plugins__dialog-actions">
            <button
              class="admin-plugins__button"
              type="button"
              autofocus
              :disabled="busy"
              @click="dismissDialog"
            >
              {{ selection.mode === 'change' ? '取消' : '关闭' }}
            </button>
            <button
              v-if="selection.mode === 'change' && error"
              class="admin-plugins__button"
              type="button"
              :disabled="busy || loading"
              @click="retry"
            >
              重新加载
            </button>
            <button
              v-if="selection.mode === 'change'"
              class="admin-plugins__button"
              :class="selection.enabled ? 'admin-plugins__button--primary' : 'admin-plugins__button--danger'"
              type="button"
              :disabled="busy || loading || !!error"
              @click="applyChange"
            >
              {{ busy ? '正在更新…' : `确认${changeVerb}` }}
            </button>
            <button
              v-else-if="selectedPlugin.installed && (!isPersonalConnector(selectedPlugin.key) || selectedPlugin.connection?.enabled)"
              class="admin-plugins__button admin-plugins__button--primary"
              type="button"
              @click="openConfig(selectedPlugin.key)"
            >
              {{ isPersonalConnector(selectedPlugin.key) ? '查看工具' : '管理配置' }}
            </button>
          </footer>
        </template>
      </dialog>
      <ConnectorConfigDialog
        v-if="managedKey"
        :key="managedKey"
        :connector-key="managedKey"
        admin-mode
        @close="closeConfig"
      />
    </template>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.admin-plugins {
  max-width: $plugin-content-width;
  margin-inline: auto;
  padding: 1.25rem $plugin-content-gutter 1.75rem;
  color: $ink;
  &--embedded { padding: 0; }

  h1, h2, p, dl, dd { margin: 0; }

  &__title {
    margin-bottom: 1.25rem;

    h1 { @include page-title; }
    p { margin-top: 0.55rem; color: $muted; font-size: 0.875rem; }
  }

  &__scope {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.85rem 0.95rem;
    border: 1px solid $line;
    border-radius: $panel-radius;
    background: linear-gradient(110deg, $selection, $paper);

    p { margin-top: 0.15rem; color: $muted; font-size: 0.8125rem; }
  }

  &__scope-icon,
  &__icon {
    display: grid;
    flex: 0 0 auto;
    width: 2.25rem;
    height: 2.25rem;
    place-items: center;
    border-radius: 8px;
    color: $green;
  }

  &__scope-icon { background: $surface; font-size: 1.5rem; line-height: 1; }

  &__icon {
    background: $selection;
    font-size: 1.1rem;

    &[data-icon='qq_mail'] { background: #e8f3db; }
    &[data-icon='tmeet'] { background: #e5edf9; }
    &[data-icon='tencent_news'] { background: #f8ece0; }
  }

  &__toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.65rem;
    margin-block: 1.35rem 0.65rem;

    h2 { font-size: 1.1rem; }
  }

  &__filters {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    min-width: 0;

    input, select {
      min-height: 2.25rem;
      padding-inline: 0.65rem;
      border: 1px solid $line;
      border-radius: $control-radius;
      background: $surface;
      color: $ink;
      font: inherit;
      font-size: 0.8125rem;
    }

    input { width: 11rem; min-width: 0; }
    input::placeholder { color: $muted; }
  }

  &__table {
    border: 1px solid $line;
    border-radius: $panel-radius;
    background: $surface;
    container-type: inline-size;
  }

  &__columns, &__row {
    display: grid;
    grid-template-columns: minmax(8rem, 2fr) minmax(6.75rem, 1fr) minmax(4.5rem, 0.75fr) minmax(6.75rem, auto);
    align-items: center;
    gap: 0.5rem;
    padding-inline: 0.8rem;
  }

  &__columns {
    min-height: 2.5rem;
    border-radius: $panel-radius $panel-radius 0 0;
    background: $sidebar;
    color: $muted;
    font-size: 0.8125rem;

    > :last-child { justify-self: end; padding-right: 0.65rem; }
  }

  &__row {
    min-height: 4rem;
    padding-block: 0.6rem;
    border-top: 1px solid $line;
  }

  &__product {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    min-width: 0;
    overflow-wrap: anywhere;

    strong { font-size: 0.925rem; }
    p { margin-top: 0.15rem; color: $muted; font-size: 0.8125rem; line-height: 1.5; }
  }

  &__auth { color: $muted; font-size: 0.8125rem; }

  &__state {
    span {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.15rem 0.45rem;
      border-radius: 5px;
      background: $sidebar;
      color: $muted;
      font-size: 0.8125rem;
      white-space: nowrap;

      &::before { width: 5px; height: 5px; border-radius: 50%; background: currentColor; content: ''; }
    }

    .admin-plugins__badge--enabled { background: $selection; color: $green; }
  }

  &__actions, &__dialog-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: end;
    gap: 0.35rem;
  }

  &__button {
    min-height: 2.125rem;
    padding: 0.25rem 0.65rem;
    border: 1px solid $line;
    border-radius: $control-radius;
    background: $surface;
    color: $ink;
    font: inherit;
    font-size: 0.8125rem;
    white-space: nowrap;
    cursor: pointer;

    &--quiet { border-color: transparent; background: transparent; color: $green; }
    &--primary { border-color: $green; background: $green; color: $on-green; font-weight: 650; }
    &--danger { border-color: $log-error; color: $log-error; font-weight: 650; }
    &:hover:not(:disabled) { border-color: $green; }
    &--primary:hover:not(:disabled) { background: $green-hover; }
    &--danger:hover:not(:disabled) { border-color: $log-error; background: $log-error-bg; }
    &:disabled { opacity: 0.6; cursor: not-allowed; }
  }

  :where(input, select, button):focus-visible { outline: 2px solid $green; outline-offset: 2px; }

  &__empty {
    padding: 1.15rem;
    border-top: 1px solid $line;
    color: $muted;
    text-align: center;
    font-size: 0.875rem;
  }

  &__feedback { display: flex; align-items: center; gap: 0.65rem; margin-bottom: 0.65rem; }
  &__error { color: $log-error; font-size: 0.875rem; }
  &__notice:not(:empty) { margin-top: 0.65rem; color: $green; font-size: 0.8125rem; }

  &__dialog {
    width: min(430px, calc(100% - 1.5rem));
    max-height: calc(100% - 1.5rem);
    padding: 1rem;
    border: 1px solid $line;
    border-radius: $panel-radius;
    background: $surface;
    color: $ink;
    box-shadow: 0 20px 50px rgb(18 38 28 / 24%);

    &::backdrop { background: rgb(16 34 24 / 46%); }
  }

  &__dialog-heading {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 0.5rem;
    padding-bottom: 0.7rem;
    border-bottom: 1px solid $line;

    h2 { font-size: 1.25rem; line-height: 1.3; overflow-wrap: anywhere; }
    p { margin-top: 0.25rem; color: $muted; font-size: 0.8125rem; }
  }

  &__detail > div {
    display: grid;
    grid-template-columns: 5.375rem minmax(0, 1fr);
    gap: 0.45rem;
    padding-block: 0.55rem;
    border-bottom: 1px solid $line;
    font-size: 0.875rem;
    overflow-wrap: anywhere;
  }

  &__detail dt, &__detail-note { color: $muted; }
  &__dialog &__detail-note { margin-top: 0.75rem; font-size: 0.8125rem; }
  &__dialog &__confirmation { margin-block: 0.85rem; font-size: 0.925rem; }
  &__dialog-actions { margin-top: 0.9rem; }
}

@container (width < 535px) {
  .admin-plugins {
    // 保留表格的列标题，便于移动端读屏识别栅格中的单元格。
    &__columns { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); }
    &__row { grid-template-columns: minmax(0, 1fr) auto; gap: 0.45rem; }
    &__row:first-child { border-top: 0; }
    &__product { grid-column: 1; grid-row: 1; }
    &__state { grid-column: 2; grid-row: 1; justify-self: end; }
    &__auth { grid-column: 1; grid-row: 2; }
    &__actions { grid-column: 2; grid-row: 2; }
  }
}

@media (max-width: 480px) {
  .admin-plugins__filters {
    width: 100%;
    input { flex: 1; }
  }
}

@media (pointer: coarse) {
  .admin-plugins__button, .admin-plugins__filters input, .admin-plugins__filters select { min-height: 44px; }
}
</style>
