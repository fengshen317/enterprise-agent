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
      :placeholder="active ? '补充要求…' : '输入问题或任务目标，键入 / 查看快捷指令'"
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
          <span
            class="chat-composer__plus-mark"
            aria-hidden="true"
          >+</span>
          <span class="chat-composer__control-label">工具</span>
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
        <span
          class="chat-composer__clip"
          aria-hidden="true"
        />
        <span class="chat-composer__control-label">附件</span>
      </button>
      <div class="chat-composer__actions">
        <select
          class="chat-composer__send-mode"
          :value="sendMode"
          aria-label="选择发送快捷键"
          :aria-describedby="hintId"
          :title="sendMode === 'enter' ? 'Shift + Enter 换行' : 'Enter 换行'"
          @change="changeSendMode"
        >
          <option value="enter">
            Enter 发送
          </option>
          <option value="modifier">
            Ctrl/⌘ + Enter 发送
          </option>
        </select>
        <button
          v-if="active"
          type="button"
          class="chat-composer__secondary chat-composer__stop"
          :disabled="stopping"
          @click="emit('stop')"
        >
          <span
            v-if="!stopping"
            class="chat-composer__stop-mark"
            aria-hidden="true"
          />
          {{ stopping ? '正在停止…' : '停止生成' }}
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
          {{ submitting ? '正在提交…' : '发送' }}
          <span
            v-if="!submitting"
            class="chat-composer__send-mark"
            aria-hidden="true"
          >↑</span>
        </button>
      </div>
    </div>
    <span
      :id="hintId"
      class="chat-composer__sr-only"
    >{{ sendMode === 'enter' ? 'Enter 发送，Shift + Enter 换行' : 'Ctrl 或 Command + Enter 发送，Enter 换行' }}</span>
    <p
      v-if="modelValue.length >= LIMIT"
      class="chat-composer__limit"
      role="status"
    >
      已达 {{ LIMIT.toLocaleString('zh-CN') }} 字符上限
    </p>
  </form>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.chat-composer {
  background: $surface;
  border: 1px solid $line;
  border-radius: 16px;
  box-shadow: 0 4px 18px rgba($ink, .06);
  padding: 8px;

  &:focus-within { border-color: $green; box-shadow: 0 0 0 3px rgba($green, .09), 0 4px 18px rgba($ink, .06); }
  :is(button, select):focus-visible { outline: 2px solid $green; outline-offset: 2px; }
  button:disabled { opacity: 1; cursor: not-allowed; }

  &__editor {
    display: block;
    width: 100%;
    min-height: 3.35em;
    max-height: max(3.35em, min(13em, 32dvh));
    padding: 6px 8px;
    border: 0;
    outline: none;
    resize: none;
    color: $ink;
    background: transparent;
    font: inherit;
    line-height: 1.65;
    scrollbar-width: thin;
    &::placeholder { color: $muted; }
  }

  &__toolbar, &__actions { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; }
  &__toolbar {
    margin-top: 4px;
    padding: 4px;
    border-radius: 10px;
    background: $paper;
  }
  &__tools { position: relative; }
  &__actions { margin-inline-start: auto; justify-content: flex-end; }
  &__plus, &__attachment, &__primary, &__secondary {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    min-height: 34px;
    padding: 4px 9px;
    border-radius: 8px;
    border: 1px solid transparent;
    font-size: 12px;
    white-space: nowrap;
  }
  &__plus, &__attachment {
    color: $green;
    background: transparent;
    &:hover:not(:disabled), &[aria-expanded='true'] { background: $selection; }
    &:disabled { color: $muted; }
  }
  &__plus-mark { font-size: 18px; line-height: 1; }
  &__clip {
    position: relative;
    width: 8px;
    height: 13px;
    border: 1px solid currentColor;
    border-radius: 5px;
    transform: rotate(36deg);

    &::after {
      position: absolute;
      content: '';
      inset: 2px 1px 1px;
      border: 1px solid currentColor;
      border-top: 0;
      border-radius: 0 0 4px 4px;
    }
  }
  &__primary {
    padding-inline: 11px;
    color: $on-green;
    background: $green;
    font-weight: 650;
    &:hover:not(:disabled) { background: $green-hover; }
    &:disabled { background: $selection; color: $muted; }
  }
  &__send-mark { font-size: 16px; line-height: 1; }
  &__secondary { color: $ink; background: $surface; border-color: $line; &:hover:not(:disabled) { background: $sidebar; } }
  &__stop { color: $log-warning; background: $log-warning-bg; &:hover:not(:disabled) { border-color: $log-warning; } }
  &__stop-mark { width: 7px; height: 7px; border-radius: 1px; background: currentColor; }
  &__panel {
    position: absolute;
    z-index: 2;
    inset-block-end: calc(100% + 8px);
    inset-inline-start: 0;
    display: grid;
    gap: 4px;
    inline-size: 236px;
    max-inline-size: calc(100vw - 56px);
    max-block-size: min(320px, 47dvh);
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 8px;
    background: $surface;
    border: 1px solid $line;
    border-radius: 9px;
    box-shadow: 0 8px 26px rgba($ink, .13);
    p { padding: 6px 8px 2px; color: $muted; font-size: 11px; }
    :deep(button) { font-size: 12px; border: 0; border-radius: 5px; text-align: left; padding: 8px; color: $ink; background: transparent; }
    :deep(button:hover:not(:disabled)) { background: $selection; }
  }
  &__send-mode {
    min-height: 34px;
    max-inline-size: 10rem;
    padding-inline: 4px;
    border: 0;
    border-radius: 8px;
    color: $muted;
    background: transparent;
    font: inherit;
    font-size: 11px;
    &:hover { background: $sidebar; color: $ink; }
  }
  &__limit { margin: 5px 4px 0; color: $log-error; font-size: 12px; text-align: end; }
  &__sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
}

@media (max-width: 480px) {
  .chat-composer {
    &__control-label { display: none; }
    &__plus, &__attachment { min-width: 38px; }
    &__editor { font-size: 16px; }
  }
}

@media (pointer: coarse) {
  .chat-composer {
    &__plus, &__attachment, &__primary, &__secondary, &__send-mode { min-height: 44px; }
    &__plus, &__attachment { min-width: 44px; }
  }
}
</style>
