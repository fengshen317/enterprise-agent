<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { getKnowledgeBase, type KnowledgeBase } from '../../api/knowledge'
import DocumentsTab from './DocumentsTab.vue'
import ParserConfigTab from './ParserConfigTab.vue'
import RetrievalTestTab from './RetrievalTestTab.vue'
import { errorLabel, statusLabel } from './knowledgeUi'

type TabKey = 'documents' | 'parser' | 'retrieval'

const route = useRoute()
const router = useRouter()
const kbId = computed(() => String(route.params.kbId ?? ''))
const kb = ref<KnowledgeBase | null>(null)
const loading = ref(false)
const error = ref('')
const tab = ref<TabKey>('documents')
let alive = true
let revision = 0

const tabs: { key: TabKey; label: string }[] = [
  { key: 'documents', label: '文档管理' },
  { key: 'parser', label: '解析配置' },
  { key: 'retrieval', label: '检索测试' },
]

function readTab(): TabKey {
  const value = String(route.query.tab ?? '')
  return value === 'parser' || value === 'retrieval' ? value : 'documents'
}

async function loadBase(): Promise<void> {
  if (!kbId.value) return
  const token = ++revision
  loading.value = true; error.value = ''
  try {
    const value = await getKnowledgeBase(kbId.value)
    if (alive && token === revision) kb.value = value
  } catch (value) { if (alive && token === revision) error.value = value instanceof Error ? value.message : '无法读取知识库' }
  finally { if (alive && token === revision) loading.value = false }
}

async function selectTab(key: TabKey): Promise<void> {
  tab.value = key
  await router.replace({ query: { ...route.query, tab: key } })
}

watch(() => route.query.tab, () => { tab.value = readTab() }, { immediate: true })
watch(kbId, loadBase, { immediate: true })
onBeforeUnmount(() => { alive = false; revision++ })
</script>

<template>
  <section class="knowledge-detail">
    <header class="knowledge-detail__heading">
      <RouterLink
        class="knowledge-detail__back"
        to="/knowledge"
      >
        ← 返回知识库列表
      </RouterLink>
      <div class="knowledge-detail__overview">
        <div class="knowledge-detail__identity">
          <h1>{{ kb?.name ?? '知识库详情' }}</h1>
          <p
            v-if="kb?.description"
            class="knowledge-detail__description"
          >
            {{ kb.description }}
          </p>
          <p
            v-if="kb"
            class="knowledge-detail__meta"
          >
            <span class="knowledge-detail__badge">{{ kb.visibility === 'workspace' ? '空间知识库' : '私有知识库' }}</span>
            <span
              class="knowledge-detail__state"
              :data-status="kb.status"
            >{{ statusLabel(kb.status) }}</span>
            <span v-if="kb.error_code">{{ errorLabel(kb.error_code) }}</span>
          </p>
        </div>
        <dl
          v-if="kb"
          class="knowledge-detail__metrics"
        >
          <div><dt>文档</dt><dd>{{ kb.document_count }}</dd></div>
          <div><dt>分块</dt><dd>{{ kb.chunk_count }}</dd></div>
        </dl>
      </div>
    </header>
    <p
      v-if="error"
      class="knowledge-detail__error"
      role="alert"
    >
      {{ error }}
    </p>
    <nav
      class="knowledge-detail__tabs"
      role="tablist"
      aria-label="知识库详情"
    >
      <button
        v-for="item in tabs"
        :key="item.key"
        type="button"
        role="tab"
        :aria-selected="tab === item.key"
        @click="selectTab(item.key)"
      >
        {{ item.label }}
      </button>
    </nav>
    <p
      v-if="loading && !kb"
      class="knowledge-detail__hint"
    >
      正在读取知识库…
    </p>
    <template v-else-if="kb">
      <DocumentsTab
        v-if="tab === 'documents'"
        :key="kb.id"
        :kb="kb"
        @changed="loadBase"
      />
      <ParserConfigTab
        v-else-if="tab === 'parser'"
        :kb="kb"
        @updated="loadBase"
      />
      <RetrievalTestTab
        v-else
        :kb="kb"
      />
    </template>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
@use '../../styles/knowledge';
.knowledge-detail {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__back { display: inline-flex; align-items: center; min-height: 34px; color: $log-muted; font-size: 13px; text-underline-offset: 3px; &:hover { color: $green; } }
  &__heading h1 { @include page-title; margin: 6px 0 12px; overflow-wrap: anywhere; }
  &__description { color: $log-muted; font-size: 14px; line-height: 1.65; margin-bottom: 10px; overflow-wrap: anywhere; }
  &__meta { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; color: $log-muted; font-size: 13px; }
  &__badge { padding: 3px 8px; border-radius: 5px; background: $selection; color: $ink; font-size: 12px; }
  &__state { @include knowledge.state; }
  &__metrics {
    display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 8px 0 0; color: $log-muted; font-size: 13px;
    div { display: flex; gap: 4px; }
    dd { margin: 0; color: $ink; font-variant-numeric: tabular-nums; }
  }
  &__tabs {
    display: flex; gap: 8px; margin: 20px 0 16px; border-bottom: 1px solid $line;
    button {
      min-height: 44px; border: 0; border-bottom: 2px solid transparent; background: none;
      color: $log-muted; padding: 8px 14px; font-size: 13px;
      &:hover { background: $sidebar; color: $ink; }
      &[aria-selected='true'] { color: $green; border-bottom-color: $green; font-weight: 600; }
    }
  }
  &__error { padding: 12px 16px; border-radius: 5px; font-size: 12px; background: $log-error-bg; color: $log-error; }
  &__hint { color: $muted; font-size: 12px; }
}
@media (min-width: $knowledge-stack-width + 1px) {
  .knowledge-detail {
    padding-top: 28px;
    &__heading { @include knowledge.desktop-header; }
    &__heading h1 { margin-bottom: 8px; }
    &__overview { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; }
    &__identity { min-width: 0; }
    &__meta { margin-top: 12px; }
    &__metrics {
      flex: none; gap: 0; max-width: 38%;
      div { flex-direction: column; min-width: 92px; gap: 4px; padding: 4px 18px; border-left: 1px solid $line; }
      dd { font-size: 24px; font-weight: 600; line-height: 1.2; }
    }
    &__tabs {
      width: fit-content; padding: 4px; gap: 4px; margin: 18px 0 22px;
      border: 1px solid $knowledge-divider; border-radius: 8px; background: $surface;
      button {
        border: 0; border-radius: 5px; min-height: 38px;
        &[aria-selected='true'] { background: $selection; color: $green; }
      }
    }
  }
}
@media (max-width: $knowledge-stack-width) {
  .knowledge-detail {
    padding: 16px 16px 32px;
    &__back { min-height: 44px; }
    &__heading h1 { font-size: 26px; }
    &__description { display: none; }
    &__tabs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; button { padding-inline: 5px; } }
  }
}
</style>
