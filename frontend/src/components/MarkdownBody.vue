<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown, renderCitations } from '../utils/markdown'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ content: string; citationIndices?: number[] }>()
const emit = defineEmits<{ cite: [index: number, trigger: HTMLElement, cited: number[]] }>()
const sanitized = computed(() => renderMarkdown(props.content))
const rendered = computed(() => renderCitations(sanitized.value, props.citationIndices ?? []))
function handleClick(event: MouseEvent): void {
  const target = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-cite]')
  if (target) emit('cite', Number(target.dataset.cite), target, rendered.value.cited)
}
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- 内容已由 DOMPurify 白名单清洗 -->
  <div
    class="markdown-body"
    v-bind="$attrs"
    @click="handleClick"
    v-html="rendered.html"
  />
  <slot
    name="after"
    :cited="rendered.cited"
  />
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.markdown-body {
  overflow-wrap: anywhere; overflow-x: auto; font-size: 14px; line-height: 1.95;
  :deep(p) { margin: 0 0 12px; }
  :deep(p:last-child) { margin-bottom: 0; }
  :deep(h1), :deep(h2), :deep(h3), :deep(h4) { margin: 18px 0 10px; font-weight: 500; line-height: 1.5; }
  :deep(h1) { font-size: 22px; }
  :deep(h2) { font-size: 19px; }
  :deep(h3) { font-size: 16px; }
  :deep(h4) { font-size: 14px; }
  :deep(ul), :deep(ol) { margin: 8px 0 12px; padding-left: 22px; }
  :deep(li) { margin: 4px 0; }
  :deep(li > p) { margin: 0; }
  :deep(table) { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }
  :deep(th), :deep(td) { border: 1px solid $line; padding: 7px 10px; text-align: left; vertical-align: top; }
  :deep(th) { background: $paper; color: $muted; font-weight: 500; }
  :deep(tr:nth-child(even)) { background: #fafbf7; }
  :deep(code) { background: #eef1e8; border-radius: 3px; padding: 1px 5px; font-family: 'Cascadia Code', Consolas, monospace; font-size: 12px; }
  :deep(pre) { background: #1f3329; color: #e6efe1; border-radius: 7px; margin: 12px 0; padding: 13px 15px; overflow-x: auto; }
  :deep(pre code) { background: none; color: inherit; padding: 0; font-size: 12px; }
  :deep(blockquote) { border-left: 3px solid $line; color: $muted; margin: 12px 0; padding: 2px 0 2px 13px; }
  :deep(a) { color: $green; text-decoration: underline; }
  :deep(hr) { border: 0; border-top: 1px solid $line; margin: 18px 0; }
  :deep(strong) { font-weight: 600; }
  :deep(.citation) { margin-inline: 2px; border: 0; border-radius: 4px; padding: 0 4px; color: $green; background: #e6efe5; font-size: 0.8em; }
  :deep(.citation:hover) { background: #d4e7d7; }
}
</style>
