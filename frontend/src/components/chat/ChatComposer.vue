<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { attachmentAccept, maxAttachmentCount } from '../../api/chatAttachments'

type SendMode = 'enter' | 'modifier'

const props = defineProps<{
  modelValue: string
  contextKey: string
  active: boolean
  loading: boolean
  submitting: boolean
  stopping: boolean
  sendMode: SendMode
  attachmentCount?: number
}>()
const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:sendMode': [mode: SendMode]
  send: []
  stop: []
  supplement: []
  attach: [files: File[]]
}>()

const LIMIT = 20_000
const shortcuts = [
  { label: '整理成表格', prompt: '请将结果整理成表格，并标明关键差异和缺失信息。' },
  { label: '生成行动计划', prompt: '请把目标拆成可执行的步骤，每步注明交付物和验收标准。' },
  { label: '按来源回答', prompt: '请优先依据可访问的知识库回答，标注来源，并说明哪些结论缺乏依据。' },
]
const editor = ref<HTMLTextAreaElement | null>(null)
const picker = ref<HTMLInputElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const panel = ref<HTMLElement | null>(null)
const tools = ref<HTMLElement | null>(null)
const openPanel = ref<'tools' | 'shortcuts' | null>(null)
const composing = ref(false)
const editorId = useId()
const panelId = useId()
const hintId = useId()
const disabled = computed(() => props.loading || props.submitting)
const hasDraft = computed(() => props.modelValue !== '/' && Boolean(props.modelValue.trim() || props.attachmentCount))

function pickFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  if (input.files?.length) emit('attach', Array.from(input.files))
  input.value = ''
}

function pasteFiles(event: ClipboardEvent): void {
  const files = event.clipboardData?.files
  if (!disabled.value && files?.length) {
    event.preventDefault()
    emit('attach', Array.from(files))
  }
}

function dragFiles(event: DragEvent): void {
  if (event.dataTransfer?.types.includes('Files')) event.preventDefault()
}

function dropFiles(event: DragEvent): void {
  if (!event.dataTransfer?.files.length) return
  event.preventDefault()
  if (!disabled.value) emit('attach', Array.from(event.dataTransfer.files))
}

function resize(): void {
  if (!editor.value) return
  editor.value.style.height = 'auto'
  editor.value.style.height = editor.value.scrollHeight + 'px'
}

async function focus(): Promise<void> {
  await nextTick()
  editor.value?.focus()
  editor.value?.setSelectionRange(editor.value.value.length, editor.value.value.length)
}

function update(event: Event): void {
  if (composing.value) return
  const value = (event.target as HTMLTextAreaElement).value
  emit('update:modelValue', value)
  openPanel.value = value === '/' ? 'shortcuts' : null
}

async function focusPanel(): Promise<void> {
  await nextTick()
  panel.value?.querySelector<HTMLElement>('a[href], select:not(:disabled), button:not(:disabled)')?.focus()
}

function toggleTools(): void {
  openPanel.value = openPanel.value === 'tools' ? null : 'tools'
  if (openPanel.value) void focusPanel()
}

function closePanel(restoreFocus = false): void {
  const previous = openPanel.value
  openPanel.value = null
  if (restoreFocus) (previous === 'shortcuts' ? editor.value : trigger.value)?.focus()
}

function templateText(prompt: string): string {
  const existing = props.modelValue.trimEnd()
  return existing && existing !== '/' ? existing + '\n\n' + prompt : prompt
}

function applyShortcut(prompt: string): void {
  const text = templateText(prompt)
  if (text.length > LIMIT) return
  emit('update:modelValue', text)
  closePanel()
  void focus()
}

function closeOnBlur(event: FocusEvent): void {
  const target = event.relatedTarget
  if (target && target !== editor.value && !tools.value?.contains(target as Node)) closePanel()
}

function closeOnOutsidePointer(event: PointerEvent): void {
  if (openPanel.value && !tools.value?.contains(event.target as Node)) closePanel()
}

function dismissOnEscape(event: KeyboardEvent): void {
  if (!openPanel.value || event.defaultPrevented) return
  event.preventDefault()
  event.stopPropagation()
  closePanel(true)
}

function submitDraft(): void {
  if (!disabled.value && !props.active && !props.stopping && hasDraft.value) emit('send')
}

function keydown(event: KeyboardEvent): void {
  if (event.isComposing || composing.value || event.keyCode === 229) return
  if (openPanel.value && event.key === 'Escape') {
    event.preventDefault()
    closePanel()
  } else if (openPanel.value && event.key === 'ArrowDown') {
    event.preventDefault()
    void focusPanel()
  } else if (event.key === 'Enter' && !event.shiftKey && !event.altKey && !props.active
    && (props.sendMode === 'modifier' ? event.ctrlKey || event.metaKey : !event.ctrlKey && !event.metaKey)) {
    event.preventDefault()
    submitDraft()
  }
}

function changeSendMode(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  emit('update:sendMode', value === 'modifier' ? 'modifier' : 'enter')
}

watch(() => props.contextKey, () => { closePanel(); composing.value = false })
watch(disabled, value => { if (value) closePanel() })
watch(() => props.modelValue, resize, { flush: 'post' })
onMounted(() => {
  resize()
  document.addEventListener('pointerdown', closeOnOutsidePointer)
})
onBeforeUnmount(() => document.removeEventListener('pointerdown', closeOnOutsidePointer))
defineExpose({ focus })
</script>

<template>
  <form
    class="chat-composer"
    @submit.prevent="submitDraft"
    @focusout="closeOnBlur"
    @dragover="dragFiles"
    @drop="dropFiles"
  >
    <slot name="attachments" />
    <label
      class="chat-composer__sr-only"
      :for="editorId"
    >输入问题</label>
    <textarea
      :id="editorId"
      ref="editor"
      class="chat-composer__editor"
      :value="modelValue"
      :placeholder="active ? '可以先编辑补充要求…' : '输入问题或任务目标，键入 / 查看快捷指令…'"
      :aria-describedby="hintId"
      :maxlength="LIMIT"
      :disabled="disabled"
      rows="2"
      @input="update"
      @compositionstart="composing = true"
      @compositionend="composing = false; update($event)"
      @keydown="keydown"
      @paste="pasteFiles"
    />
    <div class="chat-composer__toolbar">
      <div
        ref="tools"
        class="chat-composer__tools"
        @keydown.esc="dismissOnEscape"
      >
        <button
          ref="trigger"
          type="button"
          class="chat-composer__plus"
          aria-label="更多功能"
          title="更多功能"
          :aria-expanded="openPanel !== null"
          :aria-controls="openPanel ? panelId : undefined"
          :disabled="disabled"
          @click="toggleTools"
        >
          +
        </button>
        <section
          v-if="openPanel && !disabled"
          :id="panelId"
          ref="panel"
          class="chat-composer__panel"
          :aria-label="openPanel === 'tools' ? '输入框功能' : '快捷指令'"
        >
          <slot
            v-if="openPanel === 'tools'"
            name="tools"
            :close="() => closePanel(true)"
          />
          <p>/ 快捷指令 · 仅填入草稿</p>
          <button
            v-for="item in shortcuts"
            :key="item.label"
            type="button"
            :disabled="templateText(item.prompt).length > LIMIT"
            @click="applyShortcut(item.prompt)"
          >
            {{ item.label }}
          </button>
        </section>
      </div>
      <input
        ref="picker"
        type="file"
        :accept="attachmentAccept"
        multiple
        hidden
        @change="pickFiles"
      />
      <button
        type="button"
        class="chat-composer__attachment"
        aria-label="添加附件或图片"
        title="每条消息最多 4 个附件，也可粘贴图片"
        :disabled="disabled || (attachmentCount ?? 0) >= maxAttachmentCount"
        @click="picker?.click()"
      >
        <span aria-hidden="true">📎</span> 附件
      </button>
      <div class="chat-composer__actions">
        <button
          v-if="active"
          type="button"
          class="chat-composer__secondary chat-composer__stop"
          :disabled="stopping"
          @click="emit('stop')"
        >
          {{ stopping ? '正在停止…' : '■ 停止生成' }}
        </button>
        <button
          v-if="active && hasDraft"
          type="button"
          class="chat-composer__primary"
          :disabled="stopping || disabled"
          @click="emit('supplement')"
        >
          停止并补充要求
        </button>
        <button
          v-if="!active"
          type="submit"
          class="chat-composer__primary"
          :disabled="!hasDraft || disabled || stopping"
        >
          {{ submitting ? '正在提交…' : '发送 ↑' }}
        </button>
      </div>
    </div>
    <div class="chat-composer__footer">
      <select
        class="chat-composer__send-mode"
        :value="sendMode"
        aria-label="选择发送快捷键"
        @change="changeSendMode"
      >
        <option value="enter">
          Enter 发送
        </option>
        <option value="modifier">
          Ctrl/⌘ + Enter 发送
        </option>
      </select>
      <span :id="hintId">{{ sendMode === 'enter' ? 'Shift + Enter 换行' : 'Enter 换行' }}</span>
      <span
        v-if="modelValue.length >= LIMIT"
        class="chat-composer__limit"
        role="status"
      >已达 {{ LIMIT.toLocaleString('zh-CN') }} 字符上限</span>
    </div>
  </form>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.chat-composer {
  background: white;
  border: 1px solid #c7d5bc;
  border-radius: 12px;
  box-shadow: 0 3px 16px #19382f08;
  padding: 12px 14px 8px;

  &:focus-within { border-color: $green; }
  :is(button, select):focus-visible { outline: 2px solid $green; outline-offset: 2px; }
  button:disabled { cursor: not-allowed; opacity: 0.6; }

  &__editor {
    display: block;
    width: 100%;
    min-height: 3.3em;
    max-height: min(13em, 32dvh);
    padding: 2px 0;
    border: 0;
    outline: none;
    resize: none;
    color: $ink;
    background: transparent;
    font: inherit;
    line-height: 1.65;
    &::placeholder { color: $muted; }
  }

  &__toolbar, &__actions, &__footer { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }
  &__toolbar { justify-content: space-between; margin-top: 8px; }
  &__tools { position: relative; }
  &__plus {
    display: grid; place-items: center; width: 34px; height: 34px; padding: 0;
    border-radius: 50%; border: 0; color: $ink; background: #edf2e9; font-size: 23px; line-height: 1;
    &:hover:not(:disabled), &[aria-expanded='true'] { background: #dce9d8; }
  }
  &__actions { margin-inline-start: auto; justify-content: flex-end; }
  &__attachment {
    min-height: 34px; border: 1px solid $line; border-radius: 7px; padding: 4px 9px;
    background: white; color: $ink; font-size: 12px;
    &:hover:not(:disabled) { background: #edf2e9; }
  }
  &__primary, &__secondary {
    min-height: 34px;
    padding: 5px 10px;
    border-radius: 7px;
    border: 1px solid transparent;
    font-size: 12px;
  }
  &__primary { color: white; background: #31533d; &:hover:not(:disabled) { background: $ink; } }
  &__secondary { color: $ink; background: #f4f7ef; border-color: $line; &:hover:not(:disabled) { background: #eaf0e1; } }
  &__stop { color: #674623; background: #fbf2e5; }
  &__panel {
    position: absolute;
    z-index: 2;
    inset-block-end: calc(100% + 8px);
    inset-inline-start: 0;
    display: grid;
    gap: 4px;
    inline-size: 236px;
    max-inline-size: calc(100vw - 48px);
    max-block-size: min(320px, 47dvh);
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 8px;
    background: white;
    border: 1px solid $line;
    border-radius: 9px;
    box-shadow: 0 8px 26px #19382f20;
    p { padding: 6px 8px 2px; color: $muted; font-size: 11px; }
    :deep(button) { font-size: 12px; border: 0; border-radius: 5px; text-align: left; padding: 8px; color: $ink; background: transparent; }
    :deep(button:hover:not(:disabled)) { background: #eaf0e1; }
  }
  &__footer { margin-top: 7px; color: $muted; font-size: 11px; }
  &__send-mode { color: $ink; background: transparent; border: 0; font: inherit; }
  &__limit { color: #8b493c; margin-inline-start: auto; }
  &__sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
}

@media (max-width: 760px) {
  .chat-composer {
    &__plus { width: 40px; height: 40px; }
    &__primary, &__secondary { min-height: 40px; }
  }
}
</style>
