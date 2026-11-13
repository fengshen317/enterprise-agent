<script setup lang="ts">
import { computed, ref } from 'vue'
import { PARSER_TEMPLATES, type ParserForm } from './knowledgeUi'

const props = withDefaults(defineProps<{ modelValue: ParserForm; showTemplate?: boolean }>(), {
  showTemplate: true,
})
const emit = defineEmits<{ 'update:modelValue': [value: ParserForm] }>()

const templateKey = ref('')

function update<K extends keyof ParserForm>(key: K, value: ParserForm[K]): void {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function field<K extends keyof ParserForm>(key: K) {
  return computed({
    get: () => props.modelValue[key],
    set: (value: ParserForm[K]) => update(key, value),
  })
}

function textField(key: 'autoKeywordsTopN' | 'autoQuestionsTopN') {
  return computed({
    get: () => (props.modelValue[key] === '' ? '' : String(props.modelValue[key])),
    set: (value: string) => update(key, value === '' ? '' : Number(value)),
  })
}

const parserId = field('parserId')
const chunkTokenSize = field('chunkTokenSize')
const delimitersText = field('delimitersText')
const overlappedPercent = field('overlappedPercent')
const autoKeywordsTopN = textField('autoKeywordsTopN')
const autoQuestionsTopN = textField('autoQuestionsTopN')
const pdfParseMethod = computed({
  get: () => props.modelValue.pdfParseMethod,
  set: (value: string) => update('pdfParseMethod', value === 'mineru' ? 'mineru' : value === 'deepdoc' ? 'deepdoc' : ''),
})

function applyTemplate(): void {
  const template = PARSER_TEMPLATES.find(item => item.key === templateKey.value)
  if (template) emit('update:modelValue', { ...template.form })
}
</script>

<template>
  <div class="parser-fields">
    <label
      v-if="showTemplate"
      class="parser-fields__wide"
    >预设模板<select
      v-model="templateKey"
      name="parserTemplate"
      @change="applyTemplate"
    >
      <option value="">
        自定义
      </option><option
        v-for="item in PARSER_TEMPLATES"
        :key="item.key"
        :value="item.key"
      >
        {{ item.label }} · {{ item.description }}
      </option>
    </select></label>
    <label>解析器<select
      v-model="parserId"
      name="parserId"
    >
      <option value="general">
        通用
      </option><option value="manual">
        操作手册
      </option>
    </select></label>
    <label>分块 token 数<input
      v-model.number="chunkTokenSize"
      name="chunkTokenSize"
      type="number"
      min="64"
      max="4096"
    /></label>
    <label class="parser-fields__wide">分隔符（逗号分隔，换行用 \n）<input
      v-model="delimitersText"
      name="delimiters"
    /></label>
    <label>重叠比例<input
      v-model.number="overlappedPercent"
      name="overlappedPercent"
      type="number"
      min="0"
      max="0.5"
      step="0.01"
    /></label>
    <label>PDF 解析方式<select
      v-model="pdfParseMethod"
      name="pdfParseMethod"
    >
      <option value="">
        保持默认
      </option><option value="deepdoc">
        DeepDOC（通用）
      </option><option value="mineru">
        MinerU（复杂版式）
      </option>
    </select></label>
    <label>自动关键词 top_n<input
      v-model="autoKeywordsTopN"
      name="autoKeywords"
      type="number"
      min="0"
      max="30"
      placeholder="留空不修改"
    /></label>
    <label>自动问题 top_n<input
      v-model="autoQuestionsTopN"
      name="autoQuestions"
      type="number"
      min="0"
      max="30"
      placeholder="留空不修改"
    /></label>
    <p class="parser-fields__hint parser-fields__wide">
      预设模板只是参数组合；保存后写入知识库。修改解析配置不会自动重建已有索引，需要“应用到已有文档并重新解析”。
    </p>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.parser-fields {
  display: grid; grid-template-columns: 1fr 1fr; gap: 18px;
  label { color: $green; font-size: 12px; }
  input, select { display: block; width: 100%; margin-top: 8px; border: 1px solid $line; border-radius: 5px; padding: 9px 11px; background: $paper; color: $ink; font: inherit; font-size: 12px; &:focus { outline: 2px solid #92ae7a; outline-offset: 2px; } }
  &__wide { grid-column: 1 / -1; }
  &__hint { color: $muted; font-size: 11px; line-height: 1.8; }
}
@media (max-width: 760px) { .parser-fields { grid-template-columns: 1fr; } }
</style>
