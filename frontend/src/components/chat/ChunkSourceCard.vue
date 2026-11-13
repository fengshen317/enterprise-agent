<script setup lang="ts">
import { computed, ref } from 'vue'

export interface SourceChunk {
  index: number
  content: string
  document_name: string
  similarity: number | null
  document_id?: string
}

const props = defineProps<{ chunks: SourceChunk[]; collapsible?: boolean; anchorPrefix?: string }>()
const expanded = ref(!props.collapsible)

function similarityLabel(value: number | null): string {
  if (value === null || value === undefined) return '未知'
  return (value * 100).toFixed(1) + '%'
}

const summary = computed(() => {
  if (!props.chunks.length) return ''
  const names = [...new Set(props.chunks.map(c => c.document_name))].slice(0, 3)
  return `参考来源 ${props.chunks.length} 条 · ${names.join('、')}`
})

</script>

<template>
  <section
    v-if="chunks.length"
    class="chunk-sources"
    aria-label="参考来源"
  >
    <button
      v-if="collapsible"
      type="button"
      class="chunk-sources__toggle"
      @click="expanded = !expanded"
    >
      <span>{{ expanded ? '收起来源' : summary }}</span>
      <span class="chunk-sources__arrow">{{ expanded ? '▲' : '▼' }}</span>
    </button>
    <div
      v-show="expanded || !collapsible"
      class="chunk-sources__list"
    >
      <article
        v-for="chunk in chunks"
        :id="(anchorPrefix ? 'source-' + anchorPrefix + '-' : 'source-') + chunk.index"
        :key="chunk.index"
        class="chunk-sources__item"
      >
        <div class="chunk-sources__head">
          <span>[{{ chunk.index }}] {{ chunk.document_name }}</span>
          <span class="chunk-sources__score">{{ similarityLabel(chunk.similarity) }}</span>
        </div>
        <p class="chunk-sources__content">
          {{ chunk.content }}
        </p>
      </article>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.chunk-sources {
  margin: 10px 0;
  &__toggle {
    display: flex; justify-content: space-between; align-items: center; width: 100%;
    background: #f2f6ee; border: 1px solid #d6e2cc; border-radius: 6px; padding: 8px 12px;
    font-size: 11px; color: #516b42; cursor: pointer; text-align: left;
  }
  &__arrow { font-size: 9px; color: #8b9683; }
  &__list { display: grid; gap: 8px; margin-top: 8px; }
  &__item {
    border: 1px solid #e0e6da; border-radius: 6px; padding: 10px 12px; background: #fafbf8;
  }
  &__head {
    display: flex; justify-content: space-between; gap: 10px; font-size: 11px; color: $green; margin-bottom: 6px;
  }
  &__score { color: #8b9683; white-space: nowrap; }
  &__content {
    font-size: 11px; line-height: 1.7; color: #555; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0;
  }
}
</style>
