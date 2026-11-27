<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import {
  activateModelProfile, addModelProfile, discoverModelCatalog, listModelProfiles, listModelProviders, testModelProfile,
  type ConfigCheck, type ConfigItem, type ModelProfile, type ModelProfileDraft, type ModelProviderPreset,
} from '../api/config'

const props = defineProps<{
  disabled: boolean
  revision: number
  currentModel?: string
  currentProvider?: string
  currentVisionModel?: string
}>()
const emit = defineEmits<{ activated: [ConfigItem[]], activating: [boolean] }>()
const CUSTOM_PROVIDER = 'custom'
const SEARCH_THRESHOLD = 8
const PROFILE_NAME_LENGTH = 100
const labels = { text: '文本模型', vision: '图片模型', search: '联网搜索' }

const profiles = ref<ModelProfile[]>([])
const presets = ref<ModelProviderPreset[]>([])
const loading = ref(true)
const loadingPresets = ref(false)
const loadError = ref('')
const presetError = ref('')
const notice = ref('')
const activation = ref<string>()
const editing = ref(false)
const supplier = ref<HTMLSelectElement>()
const addButton = ref<HTMLButtonElement>()
const form = ref<HTMLFormElement>()
const busy = ref<'fetch' | 'test' | 'save'>()
const error = ref('')
const checks = ref<ConfigCheck[]>([])
const providerId = ref('')
const draft = ref<ModelProfileDraft>(blankDraft())
const models = ref<string[]>([])
const modelQuery = ref('')
const manualModel = ref(false)
const catalogLoaded = ref(false)

const selected = computed(() => presets.value.find(item => item.id === providerId.value))
const automatic = computed(() => selected.value?.supports_discovery === true && !manualModel.value)
const currentProfile = computed(() => profiles.value.find(item => item.active))
const filteredModels = computed(() => {
  const query = modelQuery.value.trim().toLowerCase()
  return models.value.filter(id => id === draft.value.model_id || id.toLowerCase().includes(query))
})
const currentName = computed(() => currentProfile.value?.name || props.currentModel || '尚未配置默认模型')
const currentEndpoint = computed(() => {
  const profile = currentProfile.value
  return profile && !profile.name.endsWith(profile.model_id) ? profile.model_id : props.currentProvider
})
const otherProfiles = computed(() => profiles.value.filter(item => !item.active))

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

async function loadPresets(): Promise<void> {
  if (loadingPresets.value) return
  loadingPresets.value = true
  presetError.value = ''
  try {
    presets.value = await listModelProviders()
  } catch (cause) {
    presetError.value = message(cause, '无法读取厂商列表')
  } finally {
    loadingPresets.value = false
  }
}

function clearFeedback(): void {
  error.value = ''
  checks.value = []
}

function clearCatalog(): void {
  models.value = []
  modelQuery.value = ''
  catalogLoaded.value = false
}

function changeProvider(): void {
  // 清空上一家厂商的凭据和模型，避免把旧密钥发送到新地址。
  draft.value = { ...blankDraft(), base_url: selected.value?.base_url ?? '' }
  if (providerId.value === 'deepseek') {
    draft.value.provider = 'deepseek'
    draft.value.base_url = ''
  }
  manualModel.value = false
  clearCatalog()
  clearFeedback()
}

async function open(): Promise<void> {
  providerId.value = ''
  changeProvider()
  editing.value = true
  if (!presets.value.length) void loadPresets()
  await nextTick()
  supplier.value?.focus()
}

async function close(): Promise<void> {
  editing.value = false
  providerId.value = ''
  changeProvider()
  await nextTick()
  addButton.value?.focus()
}

function keyChanged(): void {
  clearCatalog()
  if (automatic.value) {
    draft.value.model_id = ''
    draft.value.vision_model_id = ''
  }
  clearFeedback()
}

function toggleManual(): void {
  manualModel.value = !manualModel.value
  draft.value.model_id = ''
  draft.value.vision_model_id = ''
  clearFeedback()
}

async function fetchModels(): Promise<void> {
  if (!automatic.value || busy.value || props.disabled || !draft.value.api_key) return
  busy.value = 'fetch'
  clearFeedback()
  try {
    const items = await discoverModelCatalog(providerId.value, draft.value.api_key)
    models.value = items
    catalogLoaded.value = true
    if (!items.includes(draft.value.model_id)) draft.value.model_id = items.length === 1 ? items[0] : ''
    if (!items.includes(draft.value.vision_model_id)) draft.value.vision_model_id = ''
  } catch (cause) {
    error.value = message(cause, '无法获取模型列表')
  } finally {
    busy.value = undefined
  }
}

async function submit(action: 'test' | 'save'): Promise<void> {
  if (busy.value || props.disabled || activation.value || !form.value?.reportValidity() || !draft.value.model_id) return
  busy.value = action
  clearFeedback()
  try {
    const name = draft.value.name || (selected.value ? selected.value.label + ' · ' + draft.value.model_id : draft.value.model_id)
    const payload = { ...draft.value, name: Array.from(name).slice(0, PROFILE_NAME_LENGTH).join('') }
    if (action === 'test') {
      checks.value = await testModelProfile(payload)
    } else {
      const saved = await addModelProfile(payload)
      profiles.value = [saved, ...profiles.value]
      notice.value = '已保存「' + saved.name + '」。'
      await close()
    }
  } catch (cause) {
    error.value = message(cause, action === 'test' ? '连接测试失败' : '模型保存失败')
  } finally {
    busy.value = undefined
  }
}

async function activate(profile: ModelProfile): Promise<void> {
  if (props.disabled || activation.value || busy.value) return
  activation.value = profile.id
  notice.value = ''
  loadError.value = ''
  emit('activating', true)
  try {
    const configs = await activateModelProfile(profile.id)
    profiles.value = profiles.value.map(item => ({ ...item, active: item.id === profile.id }))
    emit('activated', configs)
    notice.value = '「' + profile.name + '」已设为默认模型。'
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
    <div class="model-profiles__default">
      <div>
        <p class="model-profiles__default-label">
          当前默认模型
        </p>
        <h2 id="model-profiles-title">
          {{ currentName }}
        </h2>
        <p v-if="currentEndpoint || currentProfile?.vision_model_id || currentVisionModel">
          {{ currentEndpoint }}
          <template v-if="currentProfile?.vision_model_id || currentVisionModel">
            · 图片 {{ currentProfile?.vision_model_id || currentVisionModel }}
          </template>
        </p>
      </div>
      <button
        v-if="!editing"
        ref="addButton"
        type="button"
        :disabled="disabled || !!activation"
        aria-controls="add-model-form"
        @click="open"
      >
        添加模型
      </button>
    </div>

    <form
      v-if="editing"
      id="add-model-form"
      ref="form"
      class="model-profiles__form"
      aria-labelledby="add-model-title"
      @submit.prevent="submit('save')"
    >
      <h3 id="add-model-title">
        添加模型
      </h3>
      <fieldset :disabled="disabled || !!busy || !!activation">
        <label for="model-supplier">厂商</label>
        <select
          id="model-supplier"
          ref="supplier"
          v-model="providerId"
          required
          @change="changeProvider"
        >
          <option value="">
            {{ loadingPresets ? '正在读取厂商…' : '选择 API Key 对应的厂商' }}
          </option>
          <option
            v-for="item in presets"
            :key="item.id"
            :value="item.id"
          >
            {{ item.label }}
          </option>
          <option :value="CUSTOM_PROVIDER">
            自定义 OpenAI 兼容接口
          </option>
        </select>
        <p
          v-if="presetError"
          role="alert"
          class="model-profiles__error"
        >
          {{ presetError }}
          <button
            type="button"
            class="model-profiles__text-button"
            @click="loadPresets"
          >
            重试
          </button>
        </p>
        <div
          v-if="providerId"
          class="model-profiles__credentials"
        >
          <p
            v-if="selected"
            class="model-profiles__endpoint"
          >
            {{ selected.base_url }}
            <a
              :href="selected.docs_url"
              target="_blank"
              rel="noopener noreferrer"
            >官方文档 ↗</a>
          </p>
          <label v-if="providerId === CUSTOM_PROVIDER">Base URL
            <input
              v-model.trim="draft.base_url"
              type="url"
              maxlength="500"
              required
              placeholder="https://api.example.com/v1"
              @input="clearFeedback"
            />
          </label>
          <label for="model-api-key">API Key</label>
          <div class="model-profiles__key">
            <input
              id="model-api-key"
              v-model.trim="draft.api_key"
              type="password"
              autocomplete="new-password"
              maxlength="10000"
              :required="providerId !== CUSTOM_PROVIDER"
              :placeholder="providerId === CUSTOM_PROVIDER ? '无鉴权时可留空' : '输入厂商提供的 API Key'"
              @input="keyChanged"
            />
            <button
              v-if="automatic"
              type="button"
              :disabled="!draft.api_key"
              @click="fetchModels"
            >
              {{ busy === 'fetch' ? '获取中…' : '获取模型' }}
            </button>
          </div>
          <button
            v-if="selected?.supports_discovery"
            type="button"
            class="model-profiles__text-button"
            @click="toggleManual"
          >
            {{ manualModel ? '从厂商获取模型' : '手动填写模型 ID' }}
          </button>
          <template v-if="automatic && catalogLoaded">
            <p role="status">
              {{ models.length ? '厂商返回 ' + models.length + ' 个模型' : '厂商未返回模型，可以手动填写模型 ID。' }}
            </p>
            <template v-if="models.length">
              <label v-if="models.length > SEARCH_THRESHOLD">搜索模型
                <input
                  v-model.trim="modelQuery"
                  type="search"
                  placeholder="按模型 ID 搜索"
                />
              </label>
              <label for="discovered-model">模型</label>
              <select
                id="discovered-model"
                v-model="draft.model_id"
                required
                @change="clearFeedback"
              >
                <option value="">
                  {{ filteredModels.length ? '从厂商列表选择模型' : '没有匹配的模型' }}
                </option>
                <option
                  v-for="id in filteredModels"
                  :key="id"
                  :value="id"
                >
                  {{ id }}
                </option>
              </select>
            </template>
          </template>
          <label v-if="!automatic">模型 ID
            <input
              v-model.trim="draft.model_id"
              maxlength="200"
              required
              @input="clearFeedback"
            />
          </label>
          <details v-if="draft.model_id">
            <summary>名称与图片模型</summary>
            <label>显示名称
              <input
                v-model.trim="draft.name"
                maxlength="100"
                placeholder="默认使用厂商和模型名称"
              />
            </label>
            <label v-if="draft.provider === 'openai_compatible'">图片模型（可选）
              <select
                v-if="automatic"
                v-model="draft.vision_model_id"
                @change="clearFeedback"
              >
                <option value="">不配置图片模型</option>
                <option
                  v-for="id in models"
                  :key="id"
                  :value="id"
                >
                  {{ id }}
                </option>
              </select>
              <input
                v-else
                v-model.trim="draft.vision_model_id"
                maxlength="200"
                @input="clearFeedback"
              />
            </label>
          </details>
        </div>
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
          @click="close"
        >
          取消
        </button>
        <button
          type="button"
          class="model-profiles__secondary"
          :disabled="disabled || !!busy || !!activation || !draft.model_id"
          @click="submit('test')"
        >
          {{ busy === 'test' ? '正在测试…' : '测试连接' }}
        </button>
        <button
          type="submit"
          :disabled="disabled || !!busy || !!activation || !draft.model_id"
        >
          {{ busy === 'save' ? '正在保存…' : '保存模型' }}
        </button>
      </footer>
    </form>

    <div class="model-profiles__saved">
      <h3>候选模型</h3>
      <p
        v-if="loadError"
        class="model-profiles__error"
        role="alert"
      >
        {{ loadError }}
        <button
          type="button"
          class="model-profiles__text-button"
          @click="load"
        >
          重试
        </button>
      </p>
      <p
        v-if="loading"
        role="status"
      >
        正在读取模型…
      </p>
      <p v-else-if="!otherProfiles.length && !loadError">
        添加模型后可在这里切换默认模型。
      </p>
      <ul
        v-else-if="otherProfiles.length"
        class="model-profiles__list"
      >
        <li
          v-for="profile in otherProfiles"
          :key="profile.id"
          class="model-profiles__row"
        >
          <div>
            <strong>{{ profile.name }}</strong>
            <p>{{ profile.model_id }}</p>
          </div>
          <button
            type="button"
            class="model-profiles__secondary"
            :disabled="disabled || !!activation || !!busy"
            @click="activate(profile)"
          >
            {{ activation === profile.id ? '正在切换…' : '设为默认' }}
          </button>
        </li>
      </ul>
      <p
        v-if="notice"
        class="model-profiles__notice"
        role="status"
      >
        {{ notice }}
      </p>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;

.model-profiles {
  h2, h3, p, ul { margin: 0; }
  h2 { font-size: 22px; line-height: 1.3; overflow-wrap: anywhere; }
  h3 { font-size: 16px; font-weight: 650; }
  p { color: $muted; font-size: 13px; line-height: 1.55; overflow-wrap: anywhere; }
  a { color: $green; }
  button {
    min-height: 40px; padding: 8px 14px; border: 1px solid $green;
    border-radius: $control-radius; background: $green; color: $on-green; font-size: 13px;
    &:enabled:hover { background: $green-hover; }
    &:disabled { opacity: .55; }
  }
  button.model-profiles__secondary, button.model-profiles__text-button {
    background: transparent; color: $green; border-color: $line;
    &:enabled:hover { background: $selection; }
  }
  button.model-profiles__text-button { min-height: 34px; border-color: transparent; padding-inline: 4px; justify-self: start; }
  &__default {
    display: flex; justify-content: space-between; align-items: center; gap: 16px;
    padding: 22px; border: 1px solid $line; border-radius: $panel-radius; background: $hero;
    > div { min-width: 0; }
    button { flex: none; }
    h2 + p { margin-top: 5px; }
    p.model-profiles__default-label { color: $green; margin-bottom: 5px; font-weight: 650; }
  }
  &__saved { margin-top: 25px; border-top: 1px solid $line; padding: 18px 2px 5px; }
  &__saved h3 + p { margin-top: 9px; }
  ul.model-profiles__list { padding: 0; margin-top: 8px; list-style: none; }
  &__row {
    display: flex; justify-content: space-between; align-items: center; gap: 14px;
    padding: 12px 0; border-bottom: 1px solid $line; overflow-wrap: anywhere;
    > div { min-width: 0; }
    strong { font-size: 14px; }
    button { flex: none; }
  }
  &__form {
    margin-top: 14px; padding: 20px; display: grid; gap: 12px;
    border: 1px solid $line; border-radius: $panel-radius; background: $surface;
    fieldset, .model-profiles__credentials { display: grid; gap: 9px; padding: 0; margin: 0; border: 0; min-width: 0; }
    label, summary { font-size: 13px; color: $ink; }
    label > input, label > select { display: block; margin-top: 6px; }
    input, select {
      width: 100%; max-width: 100%; min-width: 0; min-height: 40px; padding: 8px 10px;
      border: 1px solid $line; border-radius: $control-radius; background: $paper; color: $ink; font: inherit;
    }
    input::placeholder { color: $muted; }
    input[type='password']::-ms-reveal { display: none; }
    details { padding: 8px 0 0; }
    details label { display: block; margin-top: 10px; }
    summary { color: $green; cursor: pointer; }
  }
  &__endpoint { display: flex; flex-wrap: wrap; gap: 4px 12px; }
  &__key { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
  &__actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
  &__checks { padding: 0; list-style: none; color: $green; font-size: 13px; }
  p.model-profiles__notice { margin-top: 10px; color: $log-success; }
  &__error { color: $log-error !important; }
  @media (max-width: 520px) {
    &__default { align-items: flex-start; flex-direction: column; padding: 16px; }
    &__form { padding: 14px; }
    &__form input, &__form select { font-size: 16px; }
  }
}
</style>
