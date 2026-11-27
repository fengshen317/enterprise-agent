<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { listRssSources, type RssSource } from '../../api/connectors'

const props = defineProps<{ modelValue: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const sources = ref<RssSource[]>([])
const selected = ref<string[]>([])
const custom = ref('')
const loading = ref(true)
const error = ref('')
const invalid = ref(false)
let lastEmitted: string | undefined
let stopped = false

const groups = computed(() => [...new Set(sources.value.map(source => source.category))])
const sourceUrls = computed(() => new Set(sources.value.map(source => source.url)))
const urls = computed(() => [...new Set([
  ...selected.value,
  ...custom.value.split('\n').map(line => line.trim()).filter(Boolean),
])])

function sync(value: string): void {
  try {
    const parsed: unknown = JSON.parse(value || '[]')
    if (!Array.isArray(parsed) || !parsed.every(item => typeof item === 'string')) throw new Error('invalid')
    selected.value = parsed.filter(url => sourceUrls.value.has(url))
    custom.value = parsed.filter(url => !sourceUrls.value.has(url)).join('\n')
    invalid.value = false
  } catch {
    // 旧配置损坏时保留原文供修正，避免加载页面就覆盖已保存的值。
    invalid.value = true
  }
}

watch(() => props.modelValue, value => {
  if (value !== lastEmitted) sync(value)
}, { immediate: true })

function update(): void {
  lastEmitted = JSON.stringify(urls.value)
  emit('update:modelValue', lastEmitted)
}

function toggle(url: string, event: Event): void {
  if ((event.target as HTMLInputElement).checked) selected.value.push(url)
  else {
    selected.value = selected.value.filter(item => item !== url)
    custom.value = custom.value.split('\n').filter(line => line.trim() !== url).join('\n')
  }
  update()
}

function editCustom(event: Event): void {
  custom.value = (event.target as HTMLTextAreaElement).value
  update()
}

function repair(event: Event): void {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await listRssSources()
    if (stopped) return
    sources.value = result
    sync(props.modelValue)
  } catch {
    if (!stopped) error.value = '预置信源加载失败，已有订阅保留，可以重试。'
  } finally {
    if (!stopped) loading.value = false
  }
}

onMounted(() => void load())
onUnmounted(() => { stopped = true })
</script>

<template>
  <div class="rss-picker">
    <p
      v-if="loading"
      role="status"
    >
      正在加载预置信源…
    </p>
    <p
      v-if="error"
      class="rss-picker__error"
      role="alert"
    >
      {{ error }}
      <button
        type="button"
        @click="load"
      >
        重试
      </button>
    </p>
    <template v-if="invalid">
      <label class="rss-picker__label">
        已有订阅格式异常，请修正 JSON 地址数组
        <textarea
          :value="modelValue"
          :disabled="disabled"
          rows="5"
          @input="repair"
        />
      </label>
    </template>
    <template v-else-if="!loading">
      <p class="rss-picker__hint">
        请选择 1 到 10 个信源，勾选后点击保存配置。
      </p>
      <fieldset
        v-for="group in groups"
        :key="group"
        class="rss-picker__group"
        :disabled="disabled"
      >
        <legend>{{ group }}</legend>
        <label
          v-for="source in sources.filter(item => item.category === group)"
          :key="source.url"
          class="rss-picker__option"
        >
          <input
            type="checkbox"
            :value="source.url"
            :checked="urls.includes(source.url)"
            :disabled="!urls.includes(source.url) && urls.length >= 10"
            @change="toggle(source.url, $event)"
          />
          {{ source.name }}
        </label>
      </fieldset>
      <label class="rss-picker__label">
        自定义订阅（每行一个 HTTPS 地址）
        <textarea
          :value="custom"
          :disabled="disabled"
          rows="4"
          spellcheck="false"
          placeholder="https://example.com/feed.xml"
          @input="editCustom"
        />
      </label>
      <p
        v-if="urls.length > 10"
        class="rss-picker__error"
        role="alert"
      >
        最多保存 10 个信源，请减少订阅。
      </p>
    </template>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.rss-picker {
  display: grid;
  gap: 12px;
  color: $ink;

  &__hint { margin: 0; color: $muted; font-size: 12px; }
  &__group { margin: 0; padding: 12px; border: 1px solid $line; border-radius: $control-radius; }
  &__group legend { padding: 0 5px; font-size: 13px; }
  &__option { display: flex; align-items: center; gap: 8px; padding: 6px 0; font-size: 13px; }
  &__option input { accent-color: $green; }
  &__label { display: grid; gap: 8px; font-size: 13px; }
  &__label textarea {
    box-sizing: border-box;
    width: 100%;
    padding: 10px;
    resize: vertical;
    border: 1px solid $line;
    border-radius: $control-radius;
    color: $ink;
    background: $surface;
    font: inherit;
  }
  &__error { margin: 0; color: $log-error; font-size: 12px; }
}
</style>
