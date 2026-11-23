<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listConfigs, updateConfigs, type ConfigItem, type ConfigUpdate } from '../api/config'

type Provider = 'deepseek' | 'openai_compatible'
type Group = 'llm' | 'search'

interface Field {
  key: string
  label: string
  type?: 'password' | 'url' | 'number'
  placeholder?: string
  min?: number
  max?: number
  step?: string
  required?: boolean
}

const providerFields: Record<Provider, Field[]> = {
  deepseek: [
    { key: 'deepseek_model', label: '模型 ID', required: true },
    { key: 'deepseek_api_key', label: 'DeepSeek API Key', type: 'password' },
  ],
  openai_compatible: [
    { key: 'llm_base_url', label: 'Base URL', type: 'url', required: true },
    { key: 'llm_model', label: '模型 ID', placeholder: '供应商提供的模型标识', required: true },
    { key: 'llm_api_key', label: 'API Key', type: 'password', placeholder: '无鉴权时可留空' },
    { key: 'llm_vision_model', label: '图片模型 ID', placeholder: '留空时使用上方模型' },
  ],
}
const modelOptions: Field[] = [
  { key: 'model_timeout_seconds', label: '请求超时（秒）', type: 'number', min: 0.1, max: 600, step: 'any' },
  { key: 'model_max_tokens', label: '最大输出 Token', type: 'number', min: 1, max: 8192 },
]
const searchFields: Field[] = [
  { key: 'tavily_api_key', label: 'Tavily API Key', type: 'password' },
  { key: 'web_search_timeout_seconds', label: '超时（秒）', type: 'number', min: 0.1, max: 60, step: 'any' },
  { key: 'web_search_max_results', label: '最多返回结果', type: 'number', min: 1, max: 10 },
]

const configs = ref<Record<string, ConfigItem>>({})
const drafts = ref<Record<string, string>>({})
const provider = ref<Provider>('deepseek')
const loading = ref(true)
const loadError = ref('')
const busy = ref<Group | null>(null)
const feedback = ref<Record<Group, { text: string, error: boolean } | null>>({ llm: null, search: null })
const sections = computed(() => [
  { key: 'llm' as const, title: '语言模型', note: '用于问答、子 Agent、摘要和定时任务。',
    fields: [...providerFields[provider.value], ...modelOptions] },
  { key: 'search' as const, title: '联网搜索', note: '通过 Tavily 为 Agent 提供网页搜索。', fields: searchFields },
])

function apply(items: ConfigItem[]): void {
  configs.value = Object.fromEntries(items.map(item => [item.key, item]))
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    apply(await listConfigs())
    provider.value = configs.value.llm_provider?.value_masked === 'openai_compatible' ? 'openai_compatible' : 'deepseek'
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '配置读取失败'
  } finally {
    loading.value = false
  }
}

function fieldValue(field: Field): string {
  return drafts.value[field.key] ?? (field.type === 'password' ? '' : configs.value[field.key]?.value_masked ?? '')
}

function setDraft(field: Field, event: Event): void {
  const input = event.target as HTMLInputElement
  if (field.type === 'password' && !input.value) delete drafts.value[field.key]
  else drafts.value[field.key] = input.value
}

function toggleClear(key: string): void {
  if (drafts.value[key] === '') delete drafts.value[key]
  else drafts.value[key] = ''
}

async function save(group: Group, fields: Field[]): Promise<void> {
  if (busy.value) return
  const updates: ConfigUpdate[] = fields
    .filter(field => Object.hasOwn(drafts.value, field.key))
    .map(field => ({ key: field.key, value: drafts.value[field.key] }))
  if (group === 'llm' && provider.value !== configs.value.llm_provider?.value_masked) {
    updates.push({ key: 'llm_provider', value: provider.value })
  }
  if (!updates.length) {
    feedback.value[group] = { text: '配置没有变更。', error: false }
    return
  }
  busy.value = group
  feedback.value[group] = null
  try {
    apply(await updateConfigs(updates))
    for (const item of updates) delete drafts.value[item.key]
    feedback.value[group] = { text: '已保存，后续调用使用新配置。', error: false }
  } catch (error) {
    feedback.value[group] = { text: error instanceof Error ? error.message : '保存失败', error: true }
  } finally {
    busy.value = null
  }
}

onMounted(load)
</script>

<template>
  <section
    class="api-settings"
    aria-labelledby="api-settings-title"
  >
    <header class="api-settings__heading">
      <h1 id="api-settings-title">
        API 配置
      </h1>
      <p>管理工作台使用的模型与第三方服务。</p>
    </header>

    <p
      v-if="loading"
      role="status"
    >
      正在加载配置…
    </p>
    <div
      v-else-if="loadError"
      class="api-settings__panel"
    >
      <p role="alert">
        {{ loadError }}
      </p>
      <button
        type="button"
        @click="load"
      >
        重新加载
      </button>
    </div>
    <template v-else>
      <form
        v-for="section in sections"
        :key="section.key"
        class="api-settings__panel"
        @submit.prevent="save(section.key, section.fields)"
      >
        <div>
          <h2>{{ section.title }}</h2>
          <p>{{ section.note }}</p>
        </div>
        <fieldset
          :disabled="busy !== null"
          class="api-settings__fields"
        >
          <label v-if="section.key === 'llm'">
            <span>供应商</span>
            <select v-model="provider">
              <option value="deepseek">DeepSeek 官方接口</option>
              <option value="openai_compatible">OpenAI 兼容接口</option>
            </select>
          </label>
          <div
            v-for="field in section.fields"
            :key="field.key"
            class="api-settings__field"
          >
            <label>
              <span>{{ field.label }}</span>
              <input
                :value="fieldValue(field)"
                :type="field.type ?? 'text'"
                :placeholder="field.type === 'password' && configs[field.key]?.value_masked
                  ? '已配置，留空保留' : field.placeholder"
                :autocomplete="field.type === 'password' ? 'new-password' : 'off'"
                :required="field.required"
                :min="field.min"
                :max="field.max"
                :step="field.step"
                @input="setDraft(field, $event)"
              />
            </label>
            <button
              v-if="field.type === 'password' && configs[field.key]?.value_masked"
              type="button"
              class="api-settings__clear"
              @click="toggleClear(field.key)"
            >
              {{ drafts[field.key] === '' ? '撤销清除' : '清除已存密钥' }}
            </button>
          </div>
        </fieldset>
        <footer class="api-settings__actions">
          <p
            v-if="feedback[section.key]"
            :role="feedback[section.key]?.error ? 'alert' : 'status'"
            :class="{ 'api-settings__error': feedback[section.key]?.error }"
          >
            {{ feedback[section.key]?.text }}
          </p>
          <button
            type="submit"
            :disabled="busy !== null"
          >
            {{ busy === section.key ? '正在保存…' : '保存设置' }}
          </button>
        </footer>
      </form>
    </template>

    <section class="api-settings__panel api-settings__knowledge">
      <div>
        <h2>知识库 Embedding</h2>
        <p>配置新知识库用于索引和语义检索的向量模型。</p>
      </div>
      <RouterLink to="/admin/knowledge-model">管理知识库模型 <span aria-hidden="true">↗</span></RouterLink>
    </section>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;

.api-settings {
  max-width: 1050px;
  margin: 0 auto;
  padding: 38px clamp(16px, 4vw, 52px) 64px;
  color: $ink;

  h1, h2, p { margin: 0; }
  h1 { font-size: 30px; font-weight: 550; }
  h2 { font-size: 18px; font-weight: 600; }
  p { color: $muted; font-size: 13px; line-height: 1.65; }
  h1 + p, h2 + p { margin-top: 7px; }

  &__heading { margin-bottom: 25px; }
  &__panel {
    margin: 16px 0;
    padding: clamp(18px, 3vw, 26px);
    border: 1px solid $line;
    border-radius: 9px;
    background: white;
  }
  &__fields {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-items: start;
    gap: 16px 22px;
    border: 0;
    margin: 20px 0;
    padding: 0;
  }
  &__fields label { display: grid; gap: 7px; font-size: 13px; }
  input, select {
    box-sizing: border-box;
    width: 100%;
    padding: 9px 11px;
    border: 1px solid $line;
    border-radius: 5px;
    background: $paper;
    color: $ink;
    font: inherit;
  }
  button {
    padding: 8px 14px;
    border: 1px solid $green;
    border-radius: 5px;
    background: $green;
    color: white;
    font: inherit;
    font-size: 13px;
  }
  button:disabled { opacity: 0.58; }
  button.api-settings__clear {
    padding: 5px 0;
    border: 0;
    background: transparent;
    color: $green;
    font-size: 12px;
  }
  &__actions, &__knowledge { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
  &__actions { justify-content: flex-end; }
  &__knowledge { justify-content: space-between; }
  &__knowledge a { color: $green; font-size: 13px; }
  &__error { color: $log-error !important; }

  @media (max-width: 640px) {
    &__fields { grid-template-columns: minmax(0, 1fr); }
  }
}
</style>
