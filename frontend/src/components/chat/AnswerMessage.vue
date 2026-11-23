<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Message, Run } from '../../api/chat'
import MarkdownBody from '../MarkdownBody.vue'
import ToolCallList from '../ToolCallList.vue'
import type { SourceChunk } from './ChunkSourceCard.vue'

const props = defineProps<{
  message: Message
  run?: Run
  statusLabel?: string
  followUpDisabled: boolean
  progressTarget?: HTMLElement | null
}>()
const emit = defineEmits<{
  sources: [chunks: SourceChunk[], index: number | null, trigger: HTMLElement]
  followUp: []
}>()
const chunks = ref<SourceChunk[]>([])
const indices = computed(() => chunks.value.map(chunk => chunk.index))
const copyStatus = ref('')
const terminal = computed(() => props.message.status !== 'streaming')
const timestamp = computed(() => Date.parse(props.message.created_at ?? ''))
const time = computed(() => Number.isFinite(timestamp.value)
  ? new Date(timestamp.value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '')
const status = computed(() => {
  const label = props.statusLabel || (props.message.status === 'completed' ? '已完成'
    : props.message.status === 'interrupted' ? '未完成' : '正在处理')
  const start = Date.parse(props.run?.started_at ?? '')
  const end = Date.parse(props.run?.finished_at ?? '')
  return Number.isFinite(start) && end >= start ? label + ' · ' + Math.round((end - start) / 1000) + ' 秒' : label
})

function openSources(cited: number[], index: number | null, trigger: HTMLElement): void {
  const byIndex = new Map(chunks.value.map(chunk => [chunk.index, chunk]))
  const sources = cited.flatMap(number => byIndex.get(number) ?? [])
  if (sources.length) emit('sources', sources, index, trigger)
}

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.message.content)
    copyStatus.value = '已复制'
  } catch {
    copyStatus.value = '无法复制，请手动选中正文'
  }
}

watch(() => props.message.run_id, () => { chunks.value = [] })
</script>

<template>
  <header class="answer-message__heading">
    <span
      class="answer-message__avatar"
      aria-hidden="true"
    >≋</span>
    <strong>序列助手</strong>
  </header>
  <ToolCallList
    v-if="message.run_id"
    :key="message.run_id"
    :run-id="message.run_id"
    :message-status="message.status"
    :summary="status"
    :progress-target="progressTarget"
    :preload="Boolean(run) || /\[[1-9]\d*\]/.test(message.content)"
    @auto-chunks="chunks = $event"
  />
  <p
    v-if="message.status === 'interrupted' && message.content"
    class="answer-message__notice"
  >
    部分回答 · 未完成
  </p>
  <MarkdownBody
    class="chat__body answer-message__body"
    :content="message.content || (terminal ? '本次未生成正文' : '正在处理…')"
    :citation-indices="indices"
    @cite="(index, trigger, cited) => openSources(cited, index, trigger)"
  >
    <template #after="{ cited }">
      <button
        v-if="cited.length"
        type="button"
        class="answer-message__sources"
        @click="openSources(cited, null, $event.currentTarget as HTMLElement)"
      >
        查看 {{ cited.length }} 条引用 ›
      </button>
    </template>
  </MarkdownBody>
  <footer
    v-if="terminal"
    class="answer-message__footer"
  >
    <button
      v-if="message.content"
      type="button"
      @click="copy"
    >
      复制
    </button>
    <button
      v-if="message.content"
      type="button"
      :disabled="followUpDisabled"
      @click="emit('followUp')"
    >
      基于此回答追问
    </button>
    <slot name="actions" />
    <details v-if="message.run_id">
      <summary>更多</summary>
      <div class="answer-message__run-id">
        运行编号 <code>{{ message.run_id }}</code>
      </div>
    </details>
    <span class="answer-message__meta">
      <span v-if="run?.model">{{ run.model }}</span>
      <time
        v-if="time"
        :datetime="message.created_at"
      >{{ time }}</time>
    </span>
    <span
      v-if="copyStatus"
      class="answer-message__feedback"
      role="status"
    >{{ copyStatus }}</span>
  </footer>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.answer-message {
  &__heading { display: flex; align-items: center; gap: 9px; color: $ink; font-size: 14px; }
  &__avatar { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 50%; background: #dce9da; color: $green; font-size: 18px; }
  &__body { font-size: 15px; }
  &__notice { margin: 0 0 8px; color: #805226; font-size: 12px; }
  &__sources { margin-top: 14px; border: 1px solid $line; border-radius: 7px; background: #e7efe4; color: $ink; padding: 6px 10px; font-size: 12px; }
  &__footer { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 12px; margin-top: 14px; color: $muted; font-size: 12px; }
  &__footer button { border: 0; padding: 4px 0; background: none; color: $green; font: inherit; }
  &__footer :is(button:not(:disabled), summary):hover { text-decoration: underline; }
  &__footer button:disabled { color: $muted; opacity: 0.65; }
  &__footer summary { cursor: pointer; }
  &__meta { margin-inline-start: auto; display: flex; flex-wrap: wrap; gap: 4px 10px; overflow-wrap: anywhere; }
  &__run-id, &__feedback { overflow-wrap: anywhere; }
  &__feedback { flex-basis: 100%; }
}
</style>
