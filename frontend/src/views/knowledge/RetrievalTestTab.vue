<script setup lang="ts">
import { ref } from 'vue'
import { retrievalTest, type KnowledgeBase, type RetrievalTestChunk } from '../../api/knowledge'

const props = defineProps<{ kb: KnowledgeBase }>()
const query = ref('')
const topK = ref(5)
const threshold = ref(0.2)
const results = ref<RetrievalTestChunk[]>([])
const loading = ref(false)
const error = ref('')
const searched = ref(false)

function similarityLabel(value: number | null): string {
  return value === null ? '未知' : (value * 100).toFixed(1) + '%'
}

async function search(): Promise<void> {
  if (loading.value || !query.value.trim()) {
    if (!query.value.trim()) error.value = '请输入测试问题'
    return
  }
  loading.value = true; error.value = ''
  try {
    const result = await retrievalTest(props.kb.id, {
      query: query.value.trim(),
      top_k: Math.max(1, Math.min(50, Number(topK.value) || 5)),
      threshold: Math.max(0, Math.min(1, Number(threshold.value) || 0)),
    })
    results.value = result.chunks
    searched.value = true
  } catch (value) { error.value = value instanceof Error ? value.message : '检索测试失败' }
  finally { loading.value = false }
}
</script>

<template>
  <section class="retrieval-tab">
    <form
      class="retrieval-tab__form"
      @submit.prevent="search"
    >
      <h2>检索测试</h2>
      <p class="retrieval-tab__hint">
        直接测试当前知识库的召回效果；结果不写入对话，也不消耗 Agent 工具配额。
      </p>
      <label>测试问题<input
        v-model="query"
        name="retrievalQuery"
        maxlength="2000"
        required
        :disabled="loading"
        placeholder="例如：员工年假怎么计算？"
      /></label>
      <div class="retrieval-tab__params">
        <label>返回条数<input
          v-model.number="topK"
          name="retrievalTopK"
          type="number"
          min="1"
          max="50"
          :disabled="loading"
        /></label>
        <label>相似度阈值<input
          v-model.number="threshold"
          name="retrievalThreshold"
          type="number"
          min="0"
          max="1"
          step="0.05"
          :disabled="loading"
        /></label>
        <button
          type="submit"
          class="retrieval-tab__primary"
          :disabled="loading"
        >
          {{ loading ? '检索中…' : '开始检索' }}
        </button>
      </div>
    </form>
    <p
      v-if="error"
      class="retrieval-tab__error"
      role="alert"
    >
      {{ error }}
    </p>
    <section
      v-if="searched"
      class="retrieval-tab__results"
      aria-label="检索结果"
    >
      <header>
        <h2>命中结果</h2><span>{{ results.length }} 条</span>
      </header>
      <article
        v-for="(item, index) in results"
        :key="index"
        class="retrieval-tab__item"
      >
        <div class="retrieval-tab__item-head">
          <span>来源 {{ index + 1 }} · {{ item.document_name }}</span><span class="retrieval-tab__score">{{ similarityLabel(item.similarity) }}</span>
        </div>
        <p class="retrieval-tab__content">
          {{ item.content }}
        </p>
      </article>
      <p
        v-if="!results.length"
        class="retrieval-tab__empty"
      >
        没有命中结果。可以降低相似度阈值，或检查文档是否已经解析完成。
      </p>
    </section>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.retrieval-tab {
  &__form, &__results { background: white; border: 1px solid $line; border-radius: 8px; padding: 22px; margin-bottom: 18px; h2 { font-size: 17px; font-weight: 500; } }
  &__hint { color: $muted; font-size: 11px; line-height: 1.8; margin: 8px 0 16px; }
  label { display: block; color: $green; font-size: 12px; }
  input { display: block; width: 100%; margin-top: 8px; border: 1px solid $line; border-radius: 5px; padding: 9px 11px; background: $paper; color: $ink; font: inherit; font-size: 12px; &:focus { outline: 2px solid #92ae7a; outline-offset: 2px; } }
  &__params { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; margin-top: 14px; label { width: 140px; } }
  &__primary { background: $green; color: white; border: 0; border-radius: 6px; padding: 10px 16px; }
  &__results header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 14px; span { color: $muted; font-size: 11px; } }
  &__item { border-top: 1px solid $line; padding: 14px 0; }
  &__item-head { display: flex; justify-content: space-between; gap: 12px; color: $green; font-size: 12px; }
  &__score { color: $muted; font-size: 11px; }
  &__content { white-space: pre-wrap; overflow-wrap: anywhere; color: $ink; font-size: 12px; line-height: 1.9; margin-top: 10px; }
  &__error { padding: 12px 16px; border-radius: 5px; font-size: 12px; background: #faeee6; color: #934b40; }
  &__empty { color: $muted; font-size: 12px; padding: 12px 0; }
}
</style>
