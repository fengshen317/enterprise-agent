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
      <div>
        <RouterLink
          class="knowledge-detail__back"
          to="/knowledge"
        >
          ← 返回知识库列表
        </RouterLink>
        <h1>{{ kb?.name ?? '知识库详情' }}</h1>
        <p
          v-if="kb"
          class="knowledge-detail__meta"
        >
          <span class="knowledge-detail__badge">{{ kb.visibility === 'workspace' ? '空间知识库' : '私有知识库' }}</span>
          <span class="knowledge-detail__badge">{{ statusLabel(kb.status) }}</span>
          <span v-if="kb.error_code">{{ errorLabel(kb.error_code) }}</span>
          <span>文档 {{ kb.document_count }}</span>
          <span>Chunk {{ kb.chunk_count }}</span>
        </p>
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
        :class="{ 'knowledge-detail__tab--active': tab === item.key }"
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
.knowledge-detail {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__back { color: $muted; font-size: 12px; text-decoration: none; &:hover { color: $green; } }
  &__heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; h1 { font-size: 28px; font-weight: 500; margin: 13px 0; } }
  &__meta { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; color: $muted; font-size: 12px; }
  &__badge { padding: 3px 8px; border-radius: 4px; background: #eef0eb; color: $green; font-size: 10px; }
  &__tabs { display: flex; gap: 8px; margin: 26px 0 20px; border-bottom: 1px solid $line; button { border: 0; border-bottom: 2px solid transparent; background: none; color: $muted; padding: 10px 14px; font-size: 13px; } }
  &__tab--active { color: $green !important; border-bottom-color: $green !important; }
  &__error { padding: 12px 16px; border-radius: 5px; font-size: 12px; background: #faeee6; color: #934b40; }
  &__hint { color: $muted; font-size: 12px; }
}
</style>
