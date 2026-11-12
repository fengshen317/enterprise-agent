<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  reparseAllDocuments,
  updateKnowledgeBase,
  type KnowledgeBase,
  type KnowledgeBaseInput,
  type KnowledgeVisibility,
} from '../../api/knowledge'
import { useAuthStore } from '../../stores/auth'
import ParserFields from './ParserFields.vue'
import { parserForm, settingsFromForm, type ParserForm } from './knowledgeUi'

const props = defineProps<{ kb: KnowledgeBase }>()
const emit = defineEmits<{ updated: [] }>()
const auth = useAuthStore()
const canManageWorkspace = computed(() => auth.activeWorkspace?.role === 'admin')
const name = ref('')
const description = ref('')
const visibility = ref<KnowledgeVisibility>('workspace')
const parserFormValue = ref<ParserForm>(parserForm(props.kb.parser_settings, props.kb.parser_id))
const saving = ref(false)
const applying = ref(false)
const confirmApply = ref(false)
const error = ref('')
const notice = ref('')

function initialize(): void {
  name.value = props.kb.name
  description.value = props.kb.description
  visibility.value = props.kb.visibility
  parserFormValue.value = parserForm(props.kb.parser_settings, props.kb.parser_id)
}

function input(): KnowledgeBaseInput {
  return {
    name: name.value.trim(),
    description: description.value.trim(),
    visibility: visibility.value,
    parser_settings: settingsFromForm(parserFormValue.value),
    parser_id: parserFormValue.value.parserId,
    embedding_model: props.kb.embedding_model,
    language: '',
  }
}

async function save(): Promise<void> {
  if (saving.value || !name.value.trim()) {
    if (!name.value.trim()) error.value = '请填写知识库名称'
    return
  }
  saving.value = true; error.value = ''; notice.value = ''
  try {
    await updateKnowledgeBase(props.kb.id, input())
    notice.value = '解析配置已保存。新配置对之后上传的文档生效；已有文档请使用下方“应用到已有文档”。'
    emit('updated')
  } catch (value) { error.value = value instanceof Error ? value.message : '保存失败' }
  finally { saving.value = false }
}

async function applyToExisting(): Promise<void> {
  if (!confirmApply.value) { confirmApply.value = true; return }
  applying.value = true; error.value = ''; notice.value = ''
  try {
    const rows = await reparseAllDocuments(props.kb.id)
    notice.value = `已提交 ${rows.length} 个文档重新解析。刷新文档列表可查看进度。`
    confirmApply.value = false
    emit('updated')
  } catch (value) { error.value = value instanceof Error ? value.message : '应用失败' }
  finally { applying.value = false }
}

watch(() => props.kb.id, initialize, { immediate: true })
</script>

<template>
  <section class="parser-config-tab">
    <div class="parser-config-tab__card">
      <h2>基础信息</h2>
      <div class="parser-config-tab__fields">
        <label>名称<input
          v-model="name"
          name="configName"
          maxlength="100"
          :disabled="saving"
        /></label>
        <label>可见性<select
          v-model="visibility"
          name="configVisibility"
          :disabled="saving"
        >
          <option
            value="workspace"
            :disabled="!canManageWorkspace"
          >
            空间知识库
          </option><option value="private">
            私有知识库
          </option>
        </select></label>
      </div>
      <label class="parser-config-tab__description">描述<textarea
        v-model="description"
        name="configDescription"
        rows="2"
        maxlength="2000"
        :disabled="saving"
      /></label>
    </div>
    <div class="parser-config-tab__card">
      <h2>解析配置</h2>
      <p class="parser-config-tab__hint">
        解析器与分块参数会影响召回效果。保存后建议在“检索测试”里验证，再应用到已有文档。
      </p>
      <ParserFields
        v-model="parserFormValue"
        :show-template="true"
      />
      <div class="parser-config-tab__actions">
        <button
          class="parser-config-tab__primary"
          type="button"
          :disabled="saving || !name.trim()"
          @click="save"
        >
          {{ saving ? '保存中…' : '保存配置' }}
        </button>
      </div>
    </div>
    <div class="parser-config-tab__card">
      <h2>应用到已有文档</h2>
      <p class="parser-config-tab__hint">
        当前知识库有 {{ kb.document_count }} 个文档。此操作会先同步知识库默认配置，再批量重新解析已有文档；正在解析的文档也会被重新排队。
      </p>
      <div class="parser-config-tab__actions">
        <button
          type="button"
          class="parser-config-tab__primary"
          :disabled="applying || kb.document_count === 0"
          @click="applyToExisting"
        >
          {{ applying ? '提交中…' : confirmApply ? '确认应用并重新解析' : '应用到已有文档并重新解析' }}
        </button><button
          v-if="confirmApply"
          type="button"
          :disabled="applying"
          @click="confirmApply = false"
        >
          取消
        </button>
      </div>
    </div>
    <p
      v-if="error"
      class="parser-config-tab__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="parser-config-tab__notice"
      role="status"
    >
      {{ notice }}
    </p>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.parser-config-tab {
  &__card { background: white; border: 1px solid $line; border-radius: 8px; padding: 22px; margin-bottom: 18px; h2 { font-size: 17px; font-weight: 500; margin-bottom: 16px; } }
  &__fields { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; label { color: $green; font-size: 12px; } input, select { display: block; width: 100%; margin-top: 8px; } }
  input, select, textarea { border: 1px solid $line; border-radius: 5px; padding: 9px 11px; background: $paper; color: $ink; font: inherit; font-size: 12px; &:focus { outline: 2px solid #92ae7a; outline-offset: 2px; } }
  textarea { display: block; width: 100%; margin-top: 8px; resize: vertical; }
  &__description { display: block; margin-top: 18px; color: $green; font-size: 12px; }
  &__hint { color: $muted; font-size: 11px; line-height: 1.8; margin-bottom: 14px; }
  &__actions { display: flex; gap: 10px; margin-top: 18px; button:not(.parser-config-tab__primary) { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 9px 13px; color: $green; } }
  &__primary { background: $green; color: white; border: 0; border-radius: 6px; padding: 10px 16px; }
  &__error, &__notice { padding: 12px 16px; border-radius: 5px; font-size: 12px; line-height: 1.8; margin-bottom: 18px; }
  &__error { background: #faeee6; color: #934b40; }
  &__notice { background: #eaf0e1; color: $green; }
}
@media (max-width: 760px) { .parser-config-tab { &__fields { grid-template-columns: 1fr; } } }
</style>
