<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  activateModelProfile, addModelProfile, listModelProfiles, testModelProfile,
  type ConfigCheck, type ConfigItem, type ModelProfile, type ModelProfileDraft, type ModelProvider,
} from '../api/config'

const props = defineProps<{ disabled: boolean, revision: number }>()
const emit = defineEmits<{ activated: [ConfigItem[]], activating: [boolean] }>()

interface Preset { id: string, label: string, baseUrl: string, docs?: string, provider?: ModelProvider }

const presets: Preset[] = [
  { id: 'custom', label: '自定义 OpenAI 兼容接口', baseUrl: '' },
  { id: 'tencent', label: '腾讯云 TokenHub', baseUrl: 'https://tokenhub.tencentmaas.com/v1',
    docs: 'https://cloud.tencent.com/document/product/1823/138677' },
  { id: 'tencent-sg', label: '腾讯云 TokenHub（新加坡）', baseUrl: 'https://tokenhub-intl.tencentmaas.com/v1',
    docs: 'https://cloud.tencent.com/document/product/1823/138677' },
  { id: 'zhipu', label: '智谱开放平台', baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    docs: 'https://docs.bigmodel.cn/' },
  { id: 'kimi', label: 'Kimi 中国版', baseUrl: 'https://api.moonshot.cn/v1',
    docs: 'https://platform.moonshot.cn/docs/guide/start-using-kimi-api' },
  { id: 'minimax', label: 'MiniMax 中国版', baseUrl: 'https://api.minimax.cn/v1',
    docs: 'https://platform.minimax.cn/docs/api-reference/text-openai-api' },
  { id: 'deepseek', label: '深度求索 DeepSeek', baseUrl: '', provider: 'deepseek',
    docs: 'https://api-docs.deepseek.com/zh-cn/' },
]
const labels = { text: '文本模型', vision: '图片模型', search: '联网搜索' }
const defaultPreset = presets[0]
const selected = ref<Preset>(defaultPreset)
const query = ref('')
const filtered = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return presets.filter(item => `${item.label} ${item.id} ${item.baseUrl}`.toLowerCase().includes(keyword))
})
const dialog = ref<HTMLDialogElement>()
const form = ref<HTMLFormElement>()
const profiles = ref<ModelProfile[]>([])
const loading = ref(true)
const loadError = ref('')
const notice = ref('')
const activation = ref<string>()
const busy = ref<'test' | 'save'>()
const error = ref('')
const checks = ref<ConfigCheck[]>([])
const draft = ref<ModelProfileDraft>(blankDraft())

function blankDraft(): ModelProfileDraft {
  return { name: '', provider: 'openai_compatible', model_id: '', vision_model_id: '', base_url: '', api_key: '' }
}

function message(cause: unknown, fallback: string): string {
  return cause instanceof Error ? cause.message : fallback
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    profiles.value = await listModelProfiles()
  } catch (cause) {
    loadError.value = message(cause, '无法读取已保存模型')
  } finally {
    loading.value = false
  }
}

function clearFeedback(): void {
  error.value = ''
  checks.value = []
}

function open(): void {
  selected.value = defaultPreset
  query.value = ''
  draft.value = blankDraft()
  clearFeedback()
  dialog.value?.showModal()
}

function selectPreset(item: Preset): void {
  if (selected.value.id === item.id) return
  selected.value = item
  // 切换供应商时清空上一个供应商的凭据和模型 ID。
  draft.value = { ...blankDraft(), name: draft.value.name, base_url: item.baseUrl, provider: item.provider ?? 'openai_compatible' }
  clearFeedback()
}

async function submit(action: 'test' | 'save'): Promise<void> {
  if (busy.value || !form.value?.reportValidity()) return
  busy.value = action
  clearFeedback()
  try {
    const payload = { ...draft.value }
    if (action === 'test') {
      checks.value = await testModelProfile(payload)
    } else {
      const saved = await addModelProfile(payload)
      profiles.value = [saved, ...profiles.value]
      notice.value = `已添加「${saved.name}」。`
      dialog.value?.close()
    }
  } catch (cause) {
    error.value = message(cause, action === 'test' ? '连接测试失败' : '模型保存失败')
  } finally {
    busy.value = undefined
  }
}

async function activate(profile: ModelProfile): Promise<void> {
  if (props.disabled || activation.value) return
  activation.value = profile.id
  notice.value = ''
  loadError.value = ''
  emit('activating', true)
  try {
    const configs = await activateModelProfile(profile.id)
    profiles.value = profiles.value.map(item => ({ ...item, active: item.id === profile.id }))
    emit('activated', configs)
    notice.value = `「${profile.name}」已设为默认模型。`
  } catch (cause) {
    loadError.value = message(cause, '切换默认模型失败')
  } finally {
    activation.value = undefined
    emit('activating', false)
  }
}

onMounted(load)
watch(() => props.revision, load)
</script>

<template>
  <section
    class="model-profiles"
    aria-labelledby="model-profiles-title"
  >
    <header class="model-profiles__heading">
      <div>
        <h2 id="model-profiles-title">
          已保存模型
        </h2>
        <p>保存多个模型配置，按需切换工作台的默认模型。</p>
      </div>
      <button
        type="button"
        :disabled="disabled || !!activation"
        @click="open"
      >
        添加模型
      </button>
    </header>
    <p
      v-if="loadError"
      class="model-profiles__error"
      role="alert"
    >
      {{ loadError }}
    </p>
    <p
      v-if="loading"
      role="status"
    >
      正在读取模型…
    </p>
    <p
      v-else-if="!profiles.length && !loadError"
      class="model-profiles__empty"
    >
      还没有保存的模型。
    </p>
    <ul
      v-else-if="profiles.length"
      class="model-profiles__list"
    >
      <li
        v-for="profile in profiles"
        :key="profile.id"
        class="model-profiles__row"
      >
        <div class="model-profiles__identity">
          <strong>{{ profile.name }}</strong>
          <p>
            {{ profile.model_id }}<template v-if="profile.vision_model_id">
              · 图片 {{ profile.vision_model_id }}
            </template>
          </p>
        </div>
        <span
          v-if="profile.active"
          class="model-profiles__current"
        >默认模型</span>
        <button
          v-else
          type="button"
          class="model-profiles__secondary"
          :disabled="disabled || !!activation"
          @click="activate(profile)"
        >
          {{ activation === profile.id ? '正在切换…' : '设为默认' }}
        </button>
      </li>
    </ul>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>

    <dialog
      ref="dialog"
      class="model-profiles__dialog"
      aria-labelledby="add-model-title"
      @close="draft.api_key = ''"
      @cancel="busy && $event.preventDefault()"
    >
      <form
        ref="form"
        class="model-profiles__form"
        @submit.prevent="submit('save')"
        @input="clearFeedback"
      >
        <header>
          <h2 id="add-model-title">
            添加模型
          </h2>
          <p>填写供应商提供的模型 ID，可先测试连接再保存。</p>
        </header>
        <fieldset
          :disabled="!!busy"
          class="model-profiles__fields"
        >
          <div class="model-profiles__supplier">
            <div class="model-profiles__heading">
              <label for="model-provider-search">供应商</label>
              <a
                v-if="selected.docs"
                :href="selected.docs"
                target="_blank"
                rel="noopener noreferrer"
              >官方文档 ↗</a>
            </div>
            <input
              id="model-provider-search"
              v-model="query"
              type="search"
              placeholder="搜索供应商"
              autocomplete="off"
            />
            <div
              v-if="filtered.length"
              class="model-profiles__presets"
              role="group"
              aria-label="可选供应商"
            >
              <button
                v-for="item in filtered"
                :key="item.id"
                type="button"
                class="model-profiles__preset"
                :aria-pressed="selected.id === item.id"
                @click="selectPreset(item)"
              >
                {{ item.label }}
              </button>
            </div>
            <p v-else>
              没有匹配的供应商。
            </p>
            <p v-if="query && !filtered.some(item => item.id === selected.id)">
              当前供应商为 {{ selected.label }}。
            </p>
          </div>
          <label>显示名称 <input
            v-model.trim="draft.name"
            required
            maxlength="100"
            placeholder="例如 团队问答"
          /></label>
          <label>模型 ID <input
            v-model.trim="draft.model_id"
            required
            maxlength="200"
          /></label>
          <label v-if="draft.provider === 'openai_compatible'">Base URL
            <input
              v-model.trim="draft.base_url"
              required
              type="url"
              maxlength="500"
              placeholder="https://api.example.com/v1"
            />
          </label>
          <label>API Key
            <input
              v-model.trim="draft.api_key"
              type="password"
              autocomplete="new-password"
              maxlength="10000"
              :required="selected.id !== 'custom'"
              :placeholder="selected.id === 'custom' ? '无鉴权时留空' : undefined"
            />
          </label>
          <label v-if="draft.provider === 'openai_compatible'">图片模型 ID（可选）
            <input
              v-model.trim="draft.vision_model_id"
              maxlength="200"
            />
          </label>
        </fieldset>
        <p
          v-if="error"
          class="model-profiles__error"
          role="alert"
        >
          {{ error }}
        </p>
        <ul
          v-if="checks.length"
          class="model-profiles__checks"
          role="status"
        >
          <li
            v-for="result in checks"
            :key="result.check"
            :class="{ 'model-profiles__error': !result.ok }"
          >
            {{ labels[result.check] }} · {{ result.message }}
          </li>
        </ul>
        <footer class="model-profiles__actions">
          <button
            type="button"
            class="model-profiles__secondary"
            :disabled="!!busy"
            @click="dialog?.close()"
          >
            取消
          </button>
          <button
            type="button"
            class="model-profiles__secondary"
            :disabled="!!busy"
            @click="submit('test')"
          >
            {{ busy === 'test' ? '正在测试…' : '测试连接' }}
          </button>
          <button
            type="submit"
            :disabled="!!busy"
          >
            {{ busy === 'save' ? '正在保存…' : '保存模型' }}
          </button>
        </footer>
      </form>
    </dialog>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;

.model-profiles {
  margin: 16px 0;
  padding: clamp(18px, 3vw, 26px);
  border: 1px solid $line;
  border-radius: $panel-radius;
  background: $surface;

  h2, p, ul { margin: 0; }
  h2 { font-size: 18px; font-weight: 600; }
  p, a { font-size: 13px; line-height: 1.6; }
  p { color: $muted; }
  a { color: $green; }
  h2 + p { margin-top: 6px; }
  button { padding: 8px 12px; border: 1px solid $green; border-radius: $control-radius; background: $green; color: $on-green; font: inherit; font-size: 13px; }
  button:disabled { opacity: 0.58; }
  button.model-profiles__secondary { background: transparent; color: $green; }
  &__heading, &__row, &__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
  &__heading, &__row { justify-content: space-between; }
  &__list { list-style: none; padding: 0; margin-top: 13px !important; }
  &__row { padding: 11px 0; border-top: 1px solid $line; }
  &__identity { min-width: 0; overflow-wrap: anywhere; }
  &__identity strong { font-size: 14px; }
  &__current { color: $green; font-size: 13px; }
  &__empty { margin-top: 14px !important; }
  &__dialog {
    width: min(590px, calc(100% - 24px)); max-height: calc(100dvh - 24px); box-sizing: border-box;
    padding: 0; border: 1px solid $line; border-radius: $panel-radius; background: $surface; color: $ink;
  }
  &__dialog::backdrop { background: rgb(13 35 28 / 45%); }
  &__form { display: grid; gap: 14px; padding: clamp(16px, 3vw, 26px); }
  &__fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; padding: 0; margin: 0; border: 0; }
  &__fields label { display: grid; gap: 6px; font-size: 13px; }
  &__fields input {
    width: 100%; box-sizing: border-box; min-width: 0; padding: 8px 10px;
    border: 1px solid $line; border-radius: $control-radius; background: $paper; color: $ink; font: inherit;
  }
  input[type='password']::-ms-reveal { display: none; }
  &__supplier { grid-column: 1 / -1; display: grid; gap: 7px; }
  &__presets { display: flex; flex-wrap: wrap; gap: 6px; }
  button.model-profiles__preset { border-color: $line; background: $paper; color: $ink; }
  button.model-profiles__preset[aria-pressed='true'] { border-color: $green; color: $green; font-weight: 600; }
  &__actions { justify-content: flex-end; }
  &__checks { padding: 0; list-style: none; color: $green; font-size: 13px; line-height: 1.6; }
  &__error { color: $log-error !important; }
  @media (max-width: 540px) { &__fields { grid-template-columns: 1fr; } }
}
</style>
