<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import type { SourceChunk } from './ChunkSourceCard.vue'

const props = defineProps<{ chunks: SourceChunk[]; index: number | null }>()
const emit = defineEmits<{ close: [] }>()
const list = ref<HTMLElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)

function locate(): void {
  const target = list.value?.querySelector<HTMLElement>('.source-drawer__entry--selected')
  if (target && list.value) {
    list.value.scrollTop += target.getBoundingClientRect().top - list.value.getBoundingClientRect().top - 12
    target.focus({ preventScroll: true })
  } else {
    if (list.value) list.value.scrollTop = 0
    closeButton.value?.focus({ preventScroll: true })
  }
}

onMounted(locate)
watch(() => [props.chunks, props.index], locate, { flush: 'post' })
</script>

<template>
  <aside
    class="source-drawer"
    aria-label="引用来源"
    @keydown.esc.stop="emit('close')"
  >
    <header class="source-drawer__head">
      <div>
        <h2>引用来源 <span>{{ chunks.length }}</span></h2>
        <p>本条回答引用的知识库节选</p>
      </div>
      <button
        ref="closeButton"
        type="button"
        aria-label="关闭引用来源"
        @click="emit('close')"
      >
        ×
      </button>
    </header>
    <ol
      ref="list"
      class="source-drawer__list"
    >
      <li
        v-for="chunk in chunks"
        :key="chunk.index"
        class="source-drawer__entry"
        :class="{ 'source-drawer__entry--selected': index === chunk.index }"
        tabindex="-1"
      >
        <h3><span>[{{ chunk.index }}]</span> {{ chunk.document_name }}</h3>
        <p>{{ chunk.content }}</p>
      </li>
    </ol>
  </aside>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.source-drawer {
  flex: 0 0 clamp(268px, 27vw, 390px);
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid $line;
  background: #fff;
  color: $ink;
  overflow-wrap: anywhere;

  &__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 18px 20px; border-bottom: 1px solid $line; }
  h2 { font-size: 16px; span { margin-left: 4px; color: $green; font-size: 13px; } }
  &__head p { margin-top: 5px; color: $muted; font-size: 12px; }
  button { border: 0; border-radius: 6px; background: $paper; color: $ink; width: 34px; height: 34px; font-size: 20px; }
  &__list { min-height: 0; overflow-y: auto; margin: 0; padding: 12px 14px; list-style: none; overscroll-behavior: contain; }
  &__entry { padding: 12px 10px; border-bottom: 1px solid $line; }
  &__entry--selected { background: #eaf2e7; border-radius: 6px; }
  h3 { font-size: 13px; line-height: 1.5; span { color: $green; } }
  &__entry p { margin-top: 8px; color: $muted; font-size: 13px; line-height: 1.65; white-space: pre-wrap; }
}

@media (max-width: 960px) {
  .source-drawer {
    position: fixed;
    inset: auto 0 0;
    z-index: 10;
    max-height: 65dvh;
    border: 1px solid $line;
    border-radius: 16px 16px 0 0;
    box-shadow: 0 -8px 28px #19382f22;
  }
}
</style>
