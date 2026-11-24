<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import { renderMarkdown, renderCitations } from '../utils/markdown'
import { decorateMarkdownBlocks } from '../utils/markdownBlocks'
import type { highlightCode } from '../utils/codeHighlight'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ content: string; citationIndices?: number[]; enhanceBlocks?: boolean; canCopyCode?: boolean }>()
const emit = defineEmits<{ cite: [index: number, trigger: HTMLElement, cited: number[]] }>()
const sanitized = computed(() => renderMarkdown(props.content))
const rendered = computed(() => renderCitations(sanitized.value, props.citationIndices ?? []))
const highlight = shallowRef<typeof highlightCode>()
const html = computed(() => props.enhanceBlocks
  ? decorateMarkdownBlocks(rendered.value.html, props.canCopyCode === true, highlight.value)
  : rendered.value.html)
const copyAnnouncement = ref('')
let lastCopyButton: HTMLButtonElement | undefined

watch(() => props.enhanceBlocks && sanitized.value.includes('<pre><code class="language-'), (hasLanguage) => {
  if (!hasLanguage || highlight.value) return
  // 文字和表格回答无需加载语法包，加载失败时直接显示原代码。
  void import('../utils/codeHighlight').then(module => { highlight.value = module.highlightCode }).catch(() => {})
}, { immediate: true })

async function handleClick(event: MouseEvent): Promise<void> {
  const target = (event.target as Element).closest<HTMLButtonElement>('button')
  if (!target) return
  if (target.dataset.cite) {
    emit('cite', Number(target.dataset.cite), target, rendered.value.cited)
    return
  }
  if (!props.canCopyCode || !target.classList.contains('markdown-body__copy')) return

  const code = target.closest('.markdown-body__code')?.querySelector('pre > code')
  if (!code?.textContent) return
  if (lastCopyButton) lastCopyButton.textContent = '复制代码'
  lastCopyButton = target
  copyAnnouncement.value = ''
  let copied = false
  try {
    await navigator.clipboard.writeText(code.textContent)
    copied = true
  } catch {
    // 剪贴板权限受限时保留可选中的代码，便于手动复制。
  }
  if (lastCopyButton !== target || !target.isConnected) return
  target.textContent = copied ? '已复制' : '复制失败'
  copyAnnouncement.value = copied ? '代码已复制' : '复制失败，请手动选中代码'
}
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- 内容已由 DOMPurify 白名单清洗 -->
  <div
    class="markdown-body"
    v-bind="$attrs"
    @click="handleClick"
    v-html="html"
  />
  <span
    v-if="canCopyCode"
    class="markdown-body__announcement"
    role="status"
  >{{ copyAnnouncement }}</span>
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
  :deep(tr:nth-child(even)) { background: $paper; }
  :deep(code) { background: $sidebar; border-radius: 3px; padding: 1px 5px; font-family: 'Cascadia Code', Consolas, monospace; font-size: 12px; }
  :deep(pre) { background: $ink; color: $surface; border-radius: 7px; margin: 12px 0; padding: 13px 15px; overflow-x: auto; }
  :deep(pre code) { background: none; color: inherit; padding: 0; font-size: 12px; }
  :deep(blockquote) { border-left: 3px solid $line; color: $muted; margin: 12px 0; padding: 2px 0 2px 13px; }
  :deep(a) { color: $green; text-decoration: underline; }
  :deep(hr) { border: 0; border-top: 1px solid $line; margin: 18px 0; }
  :deep(strong) { font-weight: 600; }
  :deep(.citation) { margin-inline: 2px; border: 0; border-radius: 4px; padding: 0 4px; color: $green; background: $sidebar; font-size: 0.8em; }
  :deep(.citation:hover) { background: $selection; }

  :deep(.markdown-body__table) {
    max-width: 100%;
    margin: 12px 0;
    overflow-x: auto;
    border: 1px solid $line;
    border-radius: $control-radius;

    table { width: max-content; min-width: 100%; margin: 0; }
    :is(th, td) { max-width: 22rem; overflow-wrap: anywhere; }
    th { color: $ink; background: $sidebar; }
    [align='right'] { text-align: right; }
    [align='center'] { text-align: center; }
    :is(th, td):first-child { border-inline-start: 0; }
    :is(th, td):last-child { border-inline-end: 0; }
    thead th { border-top: 0; }
    tbody tr:last-child td { border-bottom: 0; }
  }

  :deep(.markdown-body__code) {
    max-width: 100%;
    margin: 12px 0;
    border: 1px solid $line;
    border-radius: $control-radius;
    background: $sidebar;

    pre { margin: 0; border-radius: 0 0 $control-radius $control-radius; }
    .hljs-comment { color: $code-comment; }
    :is(.hljs-keyword, .hljs-selector-tag, .hljs-meta) { color: $code-keyword; }
    :is(.hljs-string, .hljs-regexp, .hljs-attr) { color: $code-string; }
    :is(.hljs-number, .hljs-literal, .hljs-built_in, .hljs-title) { color: $code-value; }
  }

  :deep(.markdown-body__code-header) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 4px 10px;
    color: $ink;
    font-size: 12px;
  }

  :deep(.markdown-body__copy) {
    flex: none;
    min-height: 28px;
    padding: 2px 7px;
    border: 1px solid transparent;
    border-radius: $control-radius;
    background: transparent;
    color: $green;
    font: inherit;
    cursor: pointer;

    &:hover { background: $selection; }
  }

  :deep(:is(.markdown-body__table, .markdown-body__copy):focus-visible) {
    outline: 2px solid $green;
    outline-offset: -2px;
  }

  &__announcement {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
}
</style>
