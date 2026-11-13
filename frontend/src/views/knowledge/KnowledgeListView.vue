<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  listKnowledgeBases,
  type KnowledgeBase,
  type KnowledgeBaseInput,
  type KnowledgeVisibility,
} from '../../api/knowledge'
import { useAuthStore } from '../../stores/auth'
import ParserFields from './ParserFields.vue'
import {
  defaultParserForm,
  errorLabel,
  settingsFromForm,
  statusLabel,
  timeLabel,
  type ParserForm,
} from './knowledgeUi'

const auth = useAuthStore()
const router = useRouter()
const canManageWorkspace = computed(() => auth.activeWorkspace?.role === 'admin')
const bases = ref<KnowledgeBase[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const showForm = ref(false)
const confirmDelete = ref('')
const rowBusy = ref('')
const name = ref('')
const description = ref('')
const visibility = ref<KnowledgeVisibility>('workspace')
const parserForm = ref<ParserForm>(defaultParserForm())
let alive = true
let revision = 0

function resetForm(): void {
  name.value = ''
  description.value = ''
  visibility.value = canManageWorkspace.value ? 'workspace' : 'private'
  parserForm.value = defaultParserForm()
}

function startCreate(): void {
  showForm.value = !showForm.value
  if (showForm.value) resetForm()
}

function handleError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(): Promise<void> {
  const token = ++revision
  loading.value = true; error.value = ''
  try {
    const rows = await listKnowledgeBases()
    if (alive && token === revision) bases.value = rows
  } catch (value) { if (alive && token === revision) handleError(value) }
  finally { if (alive && token === revision) loading.value = false }
}

async function submit(): Promise<void> {
  if (saving.value || !name.value.trim()) {
    if (!name.value.trim()) error.value = '请填写知识库名称'
    return
  }
  saving.value = true; error.value = ''; notice.value = ''
  const input: KnowledgeBaseInput = {
    name: name.value.trim(),
    description: description.value.trim(),
    visibility: visibility.value,
    parser_settings: settingsFromForm(parserForm.value),
    parser_id: parserForm.value.parserId,
    embedding_model: '',
    language: '',
  }
  try {
    const created = await createKnowledgeBase(input)
    if (!alive) return
    await router.push('/knowledge/' + created.id)
  } catch (value) { if (alive) handleError(value) }
  finally { saving.value = false }
}

async function remove(item: KnowledgeBase): Promise<void> {
  if (rowBusy.value) return
  if (confirmDelete.value !== item.id) { confirmDelete.value = item.id; return }
  rowBusy.value = item.id; error.value = ''; notice.value = ''
  try {
    await deleteKnowledgeBase(item.id)
    if (!alive) return
    notice.value = '知识库已删除。'
    confirmDelete.value = ''
    await load()
  } catch (value) { if (alive) handleError(value) }
  finally { rowBusy.value = '' }
}

void load()
onBeforeUnmount(() => { alive = false; revision++ })
</script>

<template>
  <section class="knowledge-list">
    <header class="knowledge-list__heading">
      <div>
        <p class="knowledge-list__eyebrow">
          KNOWLEDGE / LIBRARY
        </p><h1>知识库</h1><p class="knowledge-list__description">
          统一管理文档解析与检索；空间管理员创建空间知识库，成员可以创建自己的私有知识库。
        </p>
      </div><button
        class="knowledge-list__primary"
        type="button"
        :disabled="saving"
        @click="startCreate"
      >
        {{ showForm ? '收起表单' : '＋ 新建知识库' }}
      </button>
    </header>
    <p class="knowledge-list__boundary">
      空间知识库对当前空间成员可检索，管理员可管理；私有知识库仅创建者可见，管理员也不能读取。
    </p>
    <p
      v-if="error"
      class="knowledge-list__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="knowledge-list__notice"
      role="status"
    >
      {{ notice }}
    </p>
    <form
      v-if="showForm"
      class="knowledge-list__form"
      @submit.prevent="submit"
    >
      <h2>新建知识库</h2>
      <div class="knowledge-list__fields">
        <label>名称<input
          v-model="name"
          name="knowledgeName"
          maxlength="100"
          required
          :disabled="saving"
          placeholder="例如：公司制度库"
        /></label>
        <label>可见性<select
          v-model="visibility"
          name="knowledgeVisibility"
          :disabled="saving"
        >
          <option
            value="workspace"
            :disabled="!canManageWorkspace"
          >
            空间知识库（管理员创建）
          </option><option value="private">
            私有知识库（仅自己）
          </option>
        </select></label>
      </div>
      <label class="knowledge-list__description-input">描述<textarea
        v-model="description"
        name="knowledgeDescription"
        rows="2"
        maxlength="2000"
        :disabled="saving"
        placeholder="这个知识库用于什么场景"
      /></label>
      <ParserFields
        v-model="parserForm"
        :show-template="true"
      />
      <div class="knowledge-list__actions">
        <button
          class="knowledge-list__primary"
          type="submit"
          :disabled="saving || !name.trim()"
        >
          {{ saving ? '创建中…' : '创建并进入详情' }}
        </button><button
          type="button"
          :disabled="saving"
          @click="showForm = false"
        >
          取消
        </button>
      </div>
    </form>
    <section
      class="knowledge-list__card"
      aria-label="知识库列表"
    >
      <div class="knowledge-list__toolbar">
        <span>我的知识库 <strong>{{ bases.length }}</strong></span><button
          type="button"
          :disabled="loading"
          @click="load"
        >
          刷新
        </button>
      </div>
      <div
        class="knowledge-list__table-wrap"
        :aria-busy="loading"
      >
        <table class="knowledge-list__table">
          <thead><tr><th>名称</th><th>可见性</th><th>状态</th><th>文档</th><th>Chunk</th><th>更新时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr
              v-for="item in bases"
              :key="item.id"
            >
              <td>
                <strong>{{ item.name }}</strong><small>{{ item.description || '暂无描述' }}</small>
              </td>
              <td>{{ item.visibility === 'workspace' ? '空间' : '私有' }}</td>
              <td>
                <span class="knowledge-list__state">{{ statusLabel(item.status) }}</span><small v-if="item.error_code">{{ errorLabel(item.error_code) }}</small>
              </td>
              <td>{{ item.document_count }}</td>
              <td>{{ item.chunk_count }}</td>
              <td>{{ timeLabel(item.updated_at) }}</td>
              <td class="knowledge-list__row-actions">
                <RouterLink :to="'/knowledge/' + item.id">
                  进入详情
                </RouterLink><button
                  type="button"
                  :disabled="!!rowBusy"
                  @click="remove(item)"
                >
                  {{ confirmDelete === item.id ? '确认删除' : '删除' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <p
          v-if="!bases.length"
          class="knowledge-list__empty"
        >
          {{ loading ? '正在读取知识库…' : '还没有知识库。' }}
        </p>
      </div>
    </section>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.knowledge-list {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 22px; h1 { font-size: 28px; font-weight: 500; margin: 13px 0; } }
  &__eyebrow { color: $muted; font-size: 10px; letter-spacing: 2px; }
  &__description, &__boundary { color: $muted; font-size: 12px; line-height: 1.9; }
  &__boundary { border-left: 2px solid $line; padding-left: 14px; margin-bottom: 26px; }
  &__primary { background: $green; color: white; border: 0; border-radius: 6px; padding: 11px 17px; }
  &__form, &__card { margin: 22px 0; background: white; border: 1px solid $line; border-radius: 8px; overflow: hidden; }
  &__form { padding: 24px; h2 { margin-bottom: 20px; font-size: 17px; font-weight: 500; } }
  &__fields { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; label { color: $green; font-size: 12px; } input, select { display: block; margin-top: 8px; width: 100%; } }
  input, select, textarea { border: 1px solid $line; border-radius: 5px; padding: 9px 11px; background: $paper; color: $ink; font: inherit; font-size: 12px; &:focus { outline: 2px solid #92ae7a; outline-offset: 2px; } }
  textarea { display: block; width: 100%; margin-top: 8px; resize: vertical; }
  &__description-input { display: block; margin: 20px 0; color: $green; font-size: 12px; }
  &__actions { display: flex; gap: 10px; margin-top: 20px; button:not(.knowledge-list__primary) { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 10px 16px; color: $green; } }
  &__toolbar { display: flex; align-items: center; justify-content: space-between; padding: 20px; span { font-size: 14px; strong { color: $muted; margin-left: 6px; } } button { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 8px 12px; color: $green; font-size: 12px; } }
  &__table-wrap { overflow-x: auto; }
  &__table { width: 100%; border-collapse: collapse; text-align: left; th { background: $paper; color: $muted; font-size: 11px; font-weight: 400; padding: 13px 20px; white-space: nowrap; } td { padding: 17px 20px; border-bottom: 1px solid $line; font-size: 12px; white-space: nowrap; vertical-align: top; } strong { font-weight: 500; } small { display: block; color: $muted; margin-top: 7px; max-width: 320px; white-space: normal; line-height: 1.7; } }
  &__state { display: inline-block; padding: 3px 7px; border-radius: 4px; background: #eef0eb; color: $muted; font-size: 10px; }
  &__row-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: flex-start; a, button { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 6px 9px; color: $green; font-size: 11px; text-decoration: none; } }
  &__error, &__notice { padding: 12px 16px; border-radius: 5px; font-size: 12px; line-height: 1.8; margin-bottom: 18px; }
  &__error { background: #faeee6; color: #934b40; }
  &__notice { background: #eaf0e1; color: $green; }
  &__empty { padding: 24px; color: $muted; font-size: 12px; }
}
@media (max-width: 900px) { .knowledge-list { padding: 24px 16px; &__fields { grid-template-columns: 1fr; } &__heading { align-items: flex-start; flex-direction: column; } } }
</style>
