<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import {
  listDocumentChunks,
  type KnowledgeChunkPage,
  type KnowledgeDocument,
} from '../../api/knowledge'

const props = defineProps<{ document: KnowledgeDocument }>()
const emit = defineEmits<{ close: [] }>()
const page = ref<KnowledgeChunkPage | null>(null)
const loading = ref(false)
const error = ref('')
const PAGE_SIZE = 20
let alive = true
let revision = 0

function handleError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '无法读取分块'
}

async function load(offset = 0): Promise<void> {
  const token = ++revision
  loading.value = true; error.value = ''
  try {
    const result = await listDocumentChunks(props.document.id, offset, PAGE_SIZE)
    if (alive && token === revision) page.value = result
  } catch (value) { if (alive && token === revision) handleError(value) }
  finally { if (alive && token === revision) loading.value = false }
}

function next(): void {
  if (!page.value || page.value.offset + PAGE_SIZE >= page.value.total) return
  void load(page.value.offset + PAGE_SIZE)
}

function previous(): void {
  if (!page.value) return
  void load(Math.max(0, page.value.offset - PAGE_SIZE))
}

watch(() => props.document.id, () => { void load(0) }, { immediate: true })
onBeforeUnmount(() => { alive = false; revision++ })
</script>

<template>
  <div
    class="chunks-dialog"
    role="dialog"
    aria-modal="true"
    aria-label="文档分块"
    @click.self="emit('close')"
  >
    <section class="chunks-dialog__panel">
      <header class="chunks-dialog__header">
        <div>
          <h2>{{ document.name }}</h2><p>只读分块列表 · 共 {{ page?.total ?? 0 }} 块</p>
        </div><button
          type="button"
          aria-label="关闭分块列表"
          @click="emit('close')"
        >
          ×
        </button>
      </header>
      <p
        v-if="error"
        class="chunks-dialog__error"
        role="alert"
      >
        {{ error }}
      </p>
      <p
        v-else-if="loading && !page"
        class="chunks-dialog__hint"
      >
        正在读取分块…
      </p>
      <ol
        v-else-if="page?.chunks.length"
        class="chunks-dialog__list"
      >
        <li
          v-for="(item, index) in page.chunks"
          :key="index"
        >
          <div class="chunks-dialog__line">
            <span>#{{ page.offset + index + 1 }}</span><span :class="{ 'chunks-dialog__disabled': !item.available }">{{ item.available ? '可检索' : '已停用' }}</span>
          </div>
          <p class="chunks-dialog__content">
            {{ item.content }}
          </p>
        </li>
      </ol>
      <p
        v-else
        class="chunks-dialog__hint"
      >
        该文档还没有可展示的分块。
      </p>
      <footer class="chunks-dialog__footer">
        <button
          type="button"
          :disabled="loading || !page || page.offset === 0"
          @click="previous"
        >
          上一页
        </button><span>{{ page ? Math.floor(page.offset / PAGE_SIZE) + 1 : 1 }} / {{ page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1 }}</span><button
          type="button"
          :disabled="loading || !page || page.offset + PAGE_SIZE >= page.total"
          @click="next"
        >
          下一页
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.chunks-dialog {
  position: fixed; inset: 0; background: #19382f66; display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 20;
  &__panel { background: white; border-radius: 9px; width: min(860px, 100%); max-height: 86vh; display: flex; flex-direction: column; overflow: hidden; }
  &__header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 20px 22px; border-bottom: 1px solid $line; h2 { font-size: 17px; font-weight: 500; overflow-wrap: anywhere; } p { color: $muted; font-size: 11px; margin-top: 6px; } button { border: 0; background: none; color: $muted; font-size: 24px; line-height: 1; } }
  &__list { list-style: none; margin: 0; padding: 0 22px; overflow-y: auto; li { border-bottom: 1px solid $line; padding: 14px 0; } }
  &__line { display: flex; justify-content: space-between; gap: 12px; color: $muted; font-size: 10px; }
  &__disabled { color: #934b40; }
  &__content { white-space: pre-wrap; overflow-wrap: anywhere; color: $ink; font-size: 12px; line-height: 1.9; margin-top: 8px; }
  &__error { margin: 18px 22px; padding: 10px 14px; background: #faeee6; color: #934b40; border-radius: 5px; font-size: 12px; }
  &__hint { padding: 24px 22px; color: $muted; font-size: 12px; }
  &__footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 22px; border-top: 1px solid $line; color: $muted; font-size: 11px; button { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 7px 11px; color: $green; font-size: 11px; } }
}
</style>
