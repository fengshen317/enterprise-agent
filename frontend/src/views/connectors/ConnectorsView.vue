<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useConnectorsStore } from '../../stores/connectors'
import {
  connectorHealthLabel,
  getConnector,
  isPersonalConnector,
  listBuiltinConnectors,
  type BuiltinConnector,
  type ConnectorView,
  type PersonalAccount,
} from '../../api/connectors'
import ConnectorConfigDialog from './ConnectorConfigDialog.vue'

defineProps<{ embedded?: boolean }>()

interface PluginCard {
  key: string
  name: string
  description: string
  icon: string
  connection?: ConnectorView
}

const MAIL_PLUGIN = 'qq_mail'
const mailChat = { path: '/chat', query: { plugin: MAIL_PLUGIN } }
const auth = useAuthStore()
const store = useConnectorsStore()
const route = useRoute()
const router = useRouter()
const builtins = ref<BuiltinConnector[]>([])
const bindings = ref<Record<string, boolean>>({})
const search = ref('')
const personalOnly = ref(false)
const catalogLoading = ref(false)
const catalogError = ref('')
const selectedKey = ref('')
const isAdmin = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
const personalKeys = computed(() => store.connectors.filter(item => isPersonalConnector(item.key))
  .map(item => item.key).sort().join(','))
let catalogRequest = 0

const cards = computed(() => {
  const result = new Map<string, PluginCard>()
  for (const item of builtins.value) result.set(item.key, item)
  for (const item of store.connectors) result.set(item.key, { ...item, connection: item })
  return [...result.values()]
})

const featuredMail = computed(() => cards.value.find(card => card.key === MAIL_PLUGIN))
const mailBound = computed(() => bindings.value[MAIL_PLUGIN] === true)
const showFeature = computed(() => featuredMail.value && !personalOnly.value && !search.value.trim())
const visibleCards = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  return cards.value.filter(card =>
    (!personalOnly.value || bindings.value[card.key] === true)
    && [card.name, card.description, card.key].some(text => text.toLocaleLowerCase().includes(query)),
  )
})

function status(card: PluginCard): string {
  if (!card.connection?.enabled) return '未开放'
  if (isPersonalConnector(card.key)) {
    if (bindings.value[card.key] === undefined) return '可绑定账号'
    return bindings.value[card.key] ? '已绑定' : '可添加'
  }
  return connectorHealthLabel(card.connection) === '健康' ? '已开放' : connectorHealthLabel(card.connection)
}

async function loadCatalog(): Promise<void> {
  const request = ++catalogRequest
  const scope = store.scope
  catalogLoading.value = true
  catalogError.value = ''
  try {
    const result = await listBuiltinConnectors()
    if (request === catalogRequest && scope === store.scope) builtins.value = result
  } catch (value) {
    if (request === catalogRequest && scope === store.scope) {
      catalogError.value = value instanceof Error ? value.message : '插件目录加载失败'
    }
  } finally {
    if (request === catalogRequest) catalogLoading.value = false
  }
}

function openDetails(card: PluginCard): void {
  selectedKey.value = card.key
}

function updateBinding(key: string, account: PersonalAccount): void {
  bindings.value = { ...bindings.value, [key]: account.bound }
}

function closeConfig(): void {
  selectedKey.value = ''
  if (route.query.connector) void router.replace({ query: { ...route.query, connector: undefined } })
}

watch(() => route.query.connector, key => {
  selectedKey.value = typeof key === 'string' ? key : ''
}, { immediate: true })

watch(() => store.scope, () => {
  bindings.value = {}
  builtins.value = []
  selectedKey.value = typeof route.query.connector === 'string' ? route.query.connector : ''
  void loadCatalog()
})

watch([() => store.scope, personalKeys], ([scope, keys], _, onCleanup) => {
  let current = true
  onCleanup(() => { current = false })
  if (!scope || !keys) return
  const requested = keys.split(',')
  void Promise.allSettled(requested.map(getConnector)).then(results => {
    if (!current || scope !== store.scope) return
    const loaded: Record<string, boolean> = {}
    for (const result of results) {
      if (result.status === 'fulfilled' && result.value.personal_account) {
        loaded[result.value.key] = result.value.personal_account.bound
      }
    }
    // 用户在抽屉修改的绑定比目录加载开始时的状态更新。
    bindings.value = { ...loaded, ...bindings.value }
  })
}, { immediate: true })

onMounted(() => {
  store.acquire()
  void loadCatalog()
})
onUnmounted(() => {
  catalogRequest++
  store.release()
})
</script>

<template>
  <section
    class="connectors"
    :class="{ 'connectors--embedded': embedded }"
    aria-label="插件中心"
  >
    <header
      v-if="!embedded"
      class="connectors__heading"
    >
      <h1>插件中心</h1>
      <p>连接常用应用，在问答中处理邮件、查找资料和跟进会议。</p>
    </header>

    <section
      v-if="showFeature && featuredMail"
      class="connectors__feature"
      aria-label="QQ 邮箱"
    >
      <div>
        <span class="connectors__feature-label">✉ 邮件</span>
        <h2>QQ 邮箱</h2>
        <p>搜索和阅读自己的收件箱，在问答中总结邮件、起草回复。</p>
        <div class="connectors__feature-actions">
          <template v-if="featuredMail.connection?.enabled">
            <RouterLink
              v-if="mailBound"
              class="connectors__primary"
              :to="mailChat"
            >
              去问答 ↗
            </RouterLink>
            <button
              type="button"
              :class="mailBound ? 'connectors__text-button' : 'connectors__primary'"
              @click="openDetails(featuredMail)"
            >
              {{ mailBound ? '管理账号' : '＋ 添加' }}
            </button>
            <span>{{ mailBound ? '凭证已保存 · 只读' : '管理员已开放 · 只读' }}</span>
          </template>
          <span v-else>管理员开放后可绑定账号</span>
        </div>
      </div>
      <div
        class="connectors__mail-art"
        aria-hidden="true"
      >
        <span class="connectors__mail-orbit" />
        <span class="connectors__mail-paper" />
        <span class="connectors__mail-envelope">✉</span>
      </div>
    </section>

    <div class="connectors__toolbar">
      <div
        class="connectors__filters"
        role="group"
        aria-label="筛选插件"
      >
        <button
          type="button"
          :aria-pressed="!personalOnly"
          @click="personalOnly = false"
        >
          全部插件
        </button>
        <button
          type="button"
          :aria-pressed="personalOnly"
          @click="personalOnly = true"
        >
          我的插件
        </button>
      </div>
      <input
        v-model="search"
        class="connectors__search"
        type="search"
        aria-label="搜索插件"
        placeholder="搜索插件"
      />
    </div>
    <div
      v-if="catalogError || store.error"
      class="connectors__feedback"
    >
      <p role="alert">
        {{ catalogError || store.error }}
      </p>
      <button
        type="button"
        class="connectors__text-button"
        @click="loadCatalog(); store.refresh(true)"
      >
        重新加载
      </button>
    </div>
    <p
      v-if="catalogLoading || store.loading"
      class="connectors__empty"
      role="status"
    >
      正在加载插件…
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
        >{{ card.key === MAIL_PLUGIN ? '✉' : card.icon || '⬡' }}</span>
        <div class="connectors__card-body">
          <h2>{{ card.name }}</h2>
          <p>{{ card.description }}</p>
          <div class="connectors__metadata">
            <span>{{ status(card) }}</span>
            <RouterLink
              v-if="card.connection?.enabled && (!isPersonalConnector(card.key) || bindings[card.key])"
              class="connectors__tools-link"
              :to="{ path: '/capabilities', query: { tab: 'tools', source: card.key } }"
            >
              查看工具
            </RouterLink>
          </div>
        </div>
        <button
          v-if="card.connection && (card.connection.enabled || isPersonalConnector(card.key))"
          class="connectors__action"
          type="button"
          :aria-label="`${isPersonalConnector(card.key) && card.connection.enabled && !bindings[card.key] ? '添加' : '查看'}${card.name}`"
          @click="openDetails(card)"
        >
          {{ isPersonalConnector(card.key) && card.connection.enabled && !bindings[card.key] ? '+' : '···' }}
        </button>
        <RouterLink
          v-else-if="isAdmin"
          class="connectors__action"
          :aria-label="`管理${card.name}`"
          :to="{ path: '/admin/plugins', query: { connector: card.key } }"
        >
          ↗
        </RouterLink>
      </article>
    </div>
    <p
      v-else-if="!catalogError && !store.error"
      class="connectors__empty"
    >
      {{ search.trim() ? '没有找到匹配的插件' : personalOnly ? '你还没有绑定个人插件' : '暂无可用插件' }}
    </p>
    <ConnectorConfigDialog
      v-if="selectedKey"
      :key="selectedKey"
      :connector-key="selectedKey"
      member-mode
      @account-updated="updateBinding(selectedKey, $event)"
      @close="closeConfig"
    />
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.connectors {
  max-width: $plugin-content-width;
  margin-inline: auto;
  padding: 1.25rem $plugin-content-gutter 1.75rem;
  color: $ink;

  &--embedded { padding: 0; }

  &__heading {
    margin-block: 0 1.2rem;

    h1 { @include page-title; margin: 0; }
    p { margin: .45rem 0 0; color: $muted; }
  }

  &__feature {
    position: relative;
    isolation: isolate;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(140px, 28%);
    align-items: center;
    overflow: hidden;
    gap: .75rem;
    padding: 1.25rem;
    min-height: 172px;
    border: 1px solid $line;
    border-radius: 13px;
    background: linear-gradient(112deg, $hero-glow, $selection);

    h2 { margin: .15rem 0 .3rem; font-size: 1.65rem; line-height: 1.2; letter-spacing: -.025em; }
    p { margin: 0; color: $muted; }
  }

  &__feature-label { font-size: .8rem; color: $green; }
  &__feature-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: .6rem;
    margin-top: .85rem;

    span { color: $muted; font-size: .8rem; }
  }

  &__primary, &__action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 2.2rem;
    padding: .35rem .7rem;
    border: 1px solid $line;
    border-radius: 7px;
    background: $surface;
    color: $ink;
    text-decoration: none;
    font: inherit;

    &:hover { border-color: $green; }
  }

  &__primary {
    border-color: $green;
    background: $green;
    color: #fff;
    font-weight: 650;

    &:hover { filter: brightness(.94); }
  }

  &__action { flex: 0 0 2.2rem; padding-inline: .2rem; font-size: 1rem; }
  &__text-button { padding: .35rem .15rem; border: 0; background: transparent; color: $green; font: inherit; }
  &__text-button:hover { text-decoration: underline; }

  &__mail-art { position: relative; display: grid; place-items: center; min-height: 125px; }
  &__mail-orbit { position: absolute; width: 140px; aspect-ratio: 1; border: 1px dashed #94b19e; border-radius: 50%; }
  &__mail-paper {
    position: absolute;
    top: 13px;
    width: 62px;
    height: 78px;
    border: 1px solid $line;
    border-radius: 6px;
    background:
      linear-gradient($selection, $selection) 10px 13px / 38px 4px no-repeat,
      linear-gradient($selection, $selection) 10px 23px / 26px 4px no-repeat,
      $surface;
    transform: rotate(11deg);
  }
  &__mail-envelope {
    position: absolute;
    top: 53px;
    width: 92px;
    height: 52px;
    display: grid;
    place-items: center;
    border-radius: 8px;
    background: $green;
    color: #fff;
    font-size: 1.45rem;
    transform: rotate(-10deg);
    box-shadow: 0 9px 20px #203b332d;
  }

  &__toolbar { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: .55rem; margin-block: 1rem .65rem; }
  &__filters { display: flex; gap: .2rem; padding: .2rem; border: 1px solid $line; border-radius: 8px; background: $sidebar; }
  &__filters button {
    min-height: 1.85rem;
    padding: .15rem .55rem;
    border: 1px solid transparent;
    border-radius: 5px;
    color: $muted;
    background: transparent;
    font: inherit;

    &[aria-pressed='true'] { background: $surface; border-color: $line; color: $ink; font-weight: 650; }
  }

  &__search { min-width: 0; width: min(100%, 15rem); min-height: 2.2rem; padding: .35rem .7rem; border: 1px solid $line; border-radius: 7px; background: $surface; color: inherit; font: inherit; }
  &__search::placeholder { color: $muted; }
  &__grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 310px), 1fr)); gap: .6rem; }
  &__card {
    display: flex;
    align-items: flex-start;
    gap: .65rem;
    min-width: 0;
    padding: .85rem;
    border: 1px solid $line;
    border-radius: 10px;
    background: $surface;

    h2 { margin: 0; font-size: 1rem; line-height: 1.3; }
    p { margin: .2rem 0 .35rem; color: $muted; font-size: .82rem; overflow-wrap: anywhere; }
  }
  &__icon { display: grid; place-items: center; flex: 0 0 2.25rem; height: 2.25rem; border-radius: 8px; background: $selection; color: $green; }
  &__card-body { flex: 1; min-width: 0; }
  &__metadata { display: flex; flex-wrap: wrap; gap: .55rem; color: $muted; font-size: .76rem; }
  &__metadata span::before { content: '•'; margin-right: .25rem; }
  &__tools-link { color: $green; }
  &__empty { padding-block: 1.3rem; color: $muted; text-align: center; }
  &__feedback { display: flex; align-items: center; flex-wrap: wrap; gap: .65rem; color: #963c2d; }

  @media (max-width: 580px) {
    &__feature { grid-template-columns: 1fr; min-height: auto; }
    &__mail-art { display: none; }
  }
}
</style>
