<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  getEmbeddingStatus,
  saveEmbedding,
  type EmbeddingPreset,
  type EmbeddingSetup,
  type EmbeddingStatus,
} from '../../api/knowledgeModel'

const labels = {
  not_connected: '等待连接',
  needs_model: '待配置',
  ready: '已启用',
}
const status = ref<EmbeddingStatus | null>(null)
const loading = ref(true)
const saving = ref(false)
const loadError = ref('')
const saveError = ref('')
const notice = ref('')
const preset = ref<EmbeddingPreset>('siliconflow_bge_m3')
const apiKey = ref('')
const baseUrl = ref('')
const modelName = ref('')
const dimension = ref<number>()
const batchSize = ref<number>()
const maxTokens = ref(8192)
const custom = computed(() => preset.value === 'compatible')
const canConfigure = computed(() => !!status.value && status.value.state !== 'not_connected'
  && !status.value.environment_override)

async function refresh(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    status.value = await getEmbeddingStatus()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '加载知识库模型失败'
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  if (!canConfigure.value || saving.value) return
  saveError.value = ''
  notice.value = ''
  saving.value = true
  const payload: EmbeddingSetup = { preset: preset.value, api_key: apiKey.value.trim() }
  if (custom.value) {
    payload.base_url = baseUrl.value.trim()
    payload.model_name = modelName.value.trim()
    payload.dimension = dimension.value
    payload.batch_size = batchSize.value
    payload.max_tokens = maxTokens.value
  }
  try {
    status.value = await saveEmbedding(payload)
    apiKey.value = ''
    notice.value = '模型验证通过，已设为新知识库的默认模型。'
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : '保存模型失败'
  } finally {
    saving.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <section
    class="knowledge-model"
    aria-labelledby="knowledge-model-title"
  >
    <header class="knowledge-model__header">
      <div>
        <h1 id="knowledge-model-title">
          知识库模型
        </h1>
        <p>配置新知识库用于索引文档和语义检索的 Embedding 模型。</p>
      </div>
      <RouterLink
        to="/knowledge"
        class="knowledge-model__link"
      >进入知识库 <span aria-hidden="true">↗</span></RouterLink>
    </header>

    <div class="knowledge-model__layout">
      <aside
        class="knowledge-model__current"
        aria-labelledby="embedding-current-title"
      >
        <div class="knowledge-model__current-header">
          <h2 id="embedding-current-title">
            当前默认模型
          </h2>
          <span
            v-if="status"
            class="knowledge-model__badge"
            :data-state="status.state"
          >{{ labels[status.state] }}</span>
        </div>
        <p
          v-if="loading"
          role="status"
        >
          正在读取模型状态…
        </p>
        <div v-else-if="loadError">
          <p role="alert">
            {{ loadError }}
          </p>
          <button
            type="button"
            class="knowledge-model__refresh"
            @click="refresh"
          >
            重新加载
          </button>
        </div>
        <template v-else>
          <dl v-if="status?.model">
            <div><dt>模型</dt><dd>{{ status.model.name }}</dd></div>
            <div><dt>供应商</dt><dd>{{ status.model.provider }}</dd></div>
          </dl>
          <p v-else-if="status?.state === 'not_connected'">
            等待 RAGFlow 服务账号初始化完成。
          </p>
          <p v-else>
            启用模型后，成员就可以创建可检索的知识库。
          </p>
          <p
            v-if="status?.environment_override"
            class="knowledge-model__override"
          >
            部署已指定 {{ status.environment_override }}。移除环境变量 EA_RAGFLOW_EMBEDDING_MODEL 后可在此管理。
          </p>
          <button
            v-if="status?.state === 'not_connected'"
            type="button"
            class="knowledge-model__refresh"
            @click="refresh"
          >
            刷新状态
          </button>
        </template>
      </aside>

      <form
        v-if="canConfigure"
        class="knowledge-model__form"
        @submit.prevent="submit"
      >
        <h2>{{ status?.state === 'ready' ? '更换默认模型' : '设置默认模型' }}</h2>
        <p class="knowledge-model__hint">
          现有知识库沿用创建时的模型。新模型通过连接测试后生效。
        </p>

        <fieldset :disabled="saving">
          <label>
            模型
            <select v-model="preset">
              <option value="siliconflow_bge_m3">硅基流动 · BAAI/bge-m3</option>
              <option value="openai_small">OpenAI · text-embedding-3-small</option>
              <option value="compatible">自定义 OpenAI 兼容接口</option>
            </select>
          </label>
          <label>
            供应商 API Key<span v-if="custom">（无鉴权时可留空）</span>
            <input
              v-model="apiKey"
              type="password"
              autocomplete="new-password"
              :required="!custom"
              maxlength="4096"
            />
          </label>
          <template v-if="custom">
            <label>
              服务地址
              <input
                v-model="baseUrl"
                type="url"
                placeholder="https://api.example.com/v1"
                maxlength="512"
                required
              />
            </label>
            <label>模型 ID<input
              v-model="modelName"
              maxlength="200"
              required
            /></label>
            <div class="knowledge-model__parameters">
              <label>向量维度<input
                v-model.number="dimension"
                type="number"
                min="1"
                max="16384"
                required
              /></label>
              <label>批量上限<input
                v-model.number="batchSize"
                type="number"
                min="1"
                max="2048"
                required
              /></label>
              <label>最大 Token 数<input
                v-model.number="maxTokens"
                type="number"
                min="1"
                max="1000000"
                required
              /></label>
            </div>
          </template>
        </fieldset>

        <p
          v-if="saveError"
          class="knowledge-model__error"
          role="alert"
        >
          {{ saveError }}
        </p>
        <p
          v-if="notice"
          class="knowledge-model__success"
          role="status"
        >
          {{ notice }}
        </p>
        <button
          type="submit"
          class="knowledge-model__submit"
          :disabled="saving"
        >
          {{ saving ? '正在测试模型…' : '验证并启用' }}
        </button>
      </form>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.knowledge-model {
  max-width: 1060px;
  margin: 0 auto;
  padding: clamp(1.5rem, 4vw, 3.25rem);
  color: $ink;

  h1, h2, p, dl, dd { margin: 0; }
  h1 { @include page-title; }
  h2 { font-size: 1.05rem; }
  p { line-height: 1.6; }
  &__header {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.7rem;
    p { margin-top: .65rem; color: $log-muted; }
  }
  &__link { color: $green; font-weight: 600; text-underline-offset: 4px; }
  &__layout {
    display: grid;
    grid-template-columns: minmax(14rem, .85fr) minmax(0, 1.45fr);
    gap: 1rem;
    align-items: start;
  }
  &__current, &__form {
    padding: clamp(1rem, 2.5vw, 1.65rem);
    border: 1px solid $line;
    border-radius: $panel-radius;
  }
  &__current {
    background: linear-gradient(110deg, $hero-glow, $hero);
    p { color: $log-muted; }
    dl div + div { margin-top: .9rem; }
    dt { color: $log-muted; font-size: .82rem; }
    dd { margin-top: .2rem; font-weight: 600; overflow-wrap: anywhere; }
  }
  &__current-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: .55rem;
    margin-bottom: 1rem;
  }
  &__badge {
    background: $log-warning-bg;
    color: $log-warning;
    border-radius: 1rem;
    padding: .25rem .6rem;
    font-size: .75rem;
    &[data-state='ready'] { background: $log-success-bg; color: $log-success; }
  }
  &__override { margin-top: .8rem !important; overflow-wrap: anywhere; }
  &__refresh { margin-top: .65rem; background: transparent; color: $green; border: 0; padding: 0; text-decoration: underline; }
  &__form {
    background: $surface;
    fieldset { display: grid; gap: .9rem; padding: 0; margin: 1.25rem 0; border: 0; }
    label { display: grid; gap: .4rem; font-size: .88rem; font-weight: 600; min-width: 0; }
    label span { color: $log-muted; font-size: .8rem; font-weight: 400; }
    input, select {
      width: 100%;
      min-width: 0;
      padding: .65rem .7rem;
      border: 1px solid $line;
      border-radius: .4rem;
      background: $surface;
      color: $ink;
      font: inherit;
      font-weight: 400;
      &:focus-visible { outline: 2px solid $green; outline-offset: 2px; }
    }
  }
  &__hint { margin-top: .45rem !important; color: $log-muted; font-size: .88rem; }
  &__parameters { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .55rem; }
  &__error, &__success { margin-bottom: .8rem !important; font-size: .88rem; }
  &__error { color: $log-error; }
  &__success { color: $log-success; }
  &__submit {
    padding: .65rem .95rem;
    border: 0;
    border-radius: .4rem;
    background: $green;
    color: $on-green;
    font-weight: 600;
    &:disabled { opacity: .62; }
  }
  @media (max-width: 760px) {
    &__layout { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 420px) {
    &__parameters { grid-template-columns: minmax(0, 1fr); }
  }
}
</style>
