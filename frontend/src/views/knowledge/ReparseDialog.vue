<script setup lang="ts">
import { ref } from 'vue'
import { parseKnowledgeDocument, type KnowledgeBase, type KnowledgeDocument } from '../../api/knowledge'
import ParserFields from './ParserFields.vue'
import { parserForm, settingsFromForm, type ParserForm } from './knowledgeUi'

const props = defineProps<{ kb: KnowledgeBase; document: KnowledgeDocument }>()
const emit = defineEmits<{ close: []; done: [] }>()
const form = ref<ParserForm>(parserForm(props.kb.parser_settings, props.kb.parser_id))
const saving = ref(false)
const error = ref('')

async function submit(): Promise<void> {
  if (saving.value) return
  saving.value = true; error.value = ''
  try {
    await parseKnowledgeDocument(props.document.id, settingsFromForm(form.value))
    emit('done')
  } catch (value) { error.value = value instanceof Error ? value.message : '重新解析失败' }
  finally { saving.value = false }
}
</script>

<template>
  <div
    class="reparse-dialog"
    role="dialog"
    aria-modal="true"
    aria-label="重新解析文档"
    @click.self="emit('close')"
  >
    <form
      class="reparse-dialog__panel"
      @submit.prevent="submit"
    >
      <header class="reparse-dialog__header">
        <div>
          <h2>重新解析</h2><p>{{ document.name }}</p>
        </div><button
          type="button"
          aria-label="关闭"
          @click="emit('close')"
        >
          ×
        </button>
      </header>
      <div class="reparse-dialog__body">
        <p class="reparse-dialog__hint">
          默认使用知识库当前配置；也可以只为本次文档覆盖解析参数。
        </p>
        <ParserFields
          v-model="form"
          :show-template="true"
        />
        <p
          v-if="error"
          class="reparse-dialog__error"
          role="alert"
        >
          {{ error }}
        </p>
      </div>
      <footer class="reparse-dialog__footer">
        <button
          type="button"
          :disabled="saving"
          @click="emit('close')"
        >
          取消
        </button><button
          type="submit"
          class="reparse-dialog__primary"
          :disabled="saving"
        >
          {{ saving ? '提交中…' : '开始重新解析' }}
        </button>
      </footer>
    </form>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.reparse-dialog {
  position: fixed; inset: 0; background: #19382f66; display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 20;
  &__panel { background: white; border-radius: 9px; width: min(760px, 100%); max-height: 88vh; display: flex; flex-direction: column; overflow: hidden; }
  &__header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 20px 22px; border-bottom: 1px solid $line; h2 { font-size: 17px; font-weight: 500; } p { color: $muted; font-size: 11px; margin-top: 6px; overflow-wrap: anywhere; } button { border: 0; background: none; color: $muted; font-size: 24px; line-height: 1; } }
  &__body { padding: 20px 22px; overflow-y: auto; }
  &__hint { color: $muted; font-size: 11px; line-height: 1.8; margin-bottom: 16px; }
  &__error { margin-top: 14px; padding: 10px 14px; background: #faeee6; color: #934b40; border-radius: 5px; font-size: 12px; }
  &__footer { display: flex; justify-content: flex-end; gap: 10px; padding: 14px 22px; border-top: 1px solid $line; button:not(.reparse-dialog__primary) { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 9px 14px; color: $green; } }
  &__primary { background: $green; color: white; border: 0; border-radius: 6px; padding: 10px 16px; }
}
</style>
