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

function resetDeleteConfirmation(event: Event, itemId: string): void {
  if (!(event.currentTarget as HTMLDetailsElement).open && confirmDelete.value === itemId) confirmDelete.value = ''
}

void load()
onBeforeUnmount(() => { alive = false; revision++ })
</script>

<template>
  <section class="knowledge-list">
    <header class="knowledge-list__heading">
      <div>
        <h1>知识库</h1>
        <p class="knowledge-list__description">
          管理供问答检索的空间资料和个人文档。
        </p>
      </div><button
        class="knowledge-list__primary"
        type="button"
        :disabled="saving"
        @click="startCreate"
      >
        <span
          v-if="!showForm"
          aria-hidden="true"
        >＋</span>{{ showForm ? '收起表单' : '新建知识库' }}
      </button>
      <p class="knowledge-list__boundary">
        空间知识库供成员检索，由空间管理员创建和管理。私有知识库仅创建者可见。
      </p>
    </header>
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
        <div class="knowledge-list__catalog-title">
          <h2>知识库目录</h2>
          <span>{{ bases.length }} 个</span>
        </div>
        <button
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
        <table
          class="knowledge-list__table"
          aria-label="知识库"
          role="table"
        >
          <thead role="rowgroup">
            <tr role="row">
              <th scope="col">
                名称
              </th><th scope="col">
                可见性
              </th><th scope="col">
                状态
              </th><th scope="col">
                文档
              </th>
              <th scope="col">
                分块
              </th><th scope="col">
                更新时间
              </th><th scope="col">
                操作
              </th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            <tr
              v-for="item in bases"
              :key="item.id"
              role="row"
            >
              <td
                class="knowledge-list__row-title"
                role="cell"
              >
                <div class="knowledge-list__item">
                  <span
                    class="knowledge-list__folder"
                    aria-hidden="true"
                  />
                  <RouterLink
                    class="knowledge-list__name"
                    :to="'/knowledge/' + item.id"
                  >
                    {{ item.name }}
                  </RouterLink>
                  <small v-if="item.description">{{ item.description }}</small>
                </div>
              </td>
              <td role="cell">
                <span class="knowledge-list__scope">{{ item.visibility === 'workspace' ? '空间知识库' : '私有知识库' }}</span>
              </td>
              <td role="cell">
                <span
                  class="knowledge-list__state"
                  :data-status="item.status"
                >{{ statusLabel(item.status) }}</span>
                <small v-if="item.error_code">{{ errorLabel(item.error_code) }}</small>
              </td>
              <td
                class="knowledge-list__count"
                role="cell"
              >
                <span
                  class="knowledge-list__mobile-label"
                  aria-hidden="true"
                >文档</span>{{ item.document_count }}
              </td>
              <td
                class="knowledge-list__count"
                role="cell"
              >
                <span
                  class="knowledge-list__mobile-label"
                  aria-hidden="true"
                >分块</span>{{ item.chunk_count }}
              </td>
              <td
                class="knowledge-list__updated"
                role="cell"
              >
                <span
                  class="knowledge-list__mobile-label"
                  aria-hidden="true"
                >更新于</span>{{ timeLabel(item.updated_at) }}
              </td>
              <td
                class="knowledge-list__row-actions"
                role="cell"
                :class="{ 'knowledge-list__row-actions--empty': !item.can_manage }"
              >
                <details
                  v-if="item.can_manage"
                  class="knowledge-list__more"
                  @toggle="resetDeleteConfirmation($event, item.id)"
                >
                  <summary :aria-label="'更多操作：' + item.name">
                    更多
                  </summary>
                  <button
                    type="button"
                    :disabled="!!rowBusy"
                    @click="remove(item)"
                  >
                    {{ confirmDelete === item.id ? '确认删除' : '删除' }}
                  </button>
                </details>
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
@use '../../styles/knowledge';
.knowledge-list {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px; h1 { @include page-title; margin-bottom: 6px; } }
  &__description, &__boundary { color: $log-muted; font-size: 13px; line-height: 1.65; }
  &__boundary { flex-basis: 100%; border-left: 2px solid $line; padding-left: 14px; }
  &__primary {
    display: inline-flex; align-items: center; gap: 6px;
    background: $green; color: $on-green; border: 0; border-radius: 7px; padding: 11px 17px;
    &:hover:not(:disabled) { background: $ink; }
  }
  &__form, &__card { margin: 22px 0; background: $surface; border: 1px solid $line; border-radius: $panel-radius; overflow: hidden; }
  &__form { padding: 24px; h2 { margin-bottom: 20px; font-size: 17px; font-weight: 500; } }
  &__fields { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; label { color: $green; font-size: 12px; } input, select { display: block; margin-top: 8px; width: 100%; } }
  input, select, textarea { border: 1px solid $line; border-radius: 5px; padding: 9px 11px; background: $paper; color: $ink; font: inherit; font-size: 12px; &:focus { outline: 2px solid $green; outline-offset: 2px; } }
  textarea { display: block; width: 100%; margin-top: 8px; resize: vertical; }
  &__description-input { display: block; margin: 20px 0; color: $green; font-size: 12px; }
  &__actions { display: flex; gap: 10px; margin-top: 20px; button:not(.knowledge-list__primary) { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 10px 16px; color: $green; } }
  &__toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 20px; button { @include knowledge.row-action; } }
  &__catalog-title {
    display: flex; align-items: center; gap: 10px;
    h2 { font-size: 16px; font-weight: 600; }
    span { border-radius: 5px; background: $knowledge-tint; color: $log-success; padding: 3px 7px; font-size: 12px; }
  }
  &__table {
    @include knowledge.table(13px 12px);
    th:nth-child(1) { width: 29%; }
    th:nth-child(2), th:nth-child(7) { width: 11%; }
    th:nth-child(3) { width: 14%; }
    th:nth-child(4), th:nth-child(5) { width: 8%; }
    th:nth-child(6) { width: 19%; }
  }
  &__folder { display: none; }
  &__name { color: $ink; font-size: 15px; font-weight: 600; text-underline-offset: 3px; &:hover { color: $green; } }
  &__scope { color: $log-muted; }
  &__state { @include knowledge.state; }
  &__count, &__updated { font-variant-numeric: tabular-nums; }
  &__mobile-label { display: none; }
  &__more {
    @include knowledge.disclosure;
    button { @include knowledge.row-action; margin-top: 6px; color: $log-error; }
  }
  &__error, &__notice { padding: 12px 16px; border-radius: 5px; font-size: 12px; line-height: 1.8; margin-bottom: 18px; }
  &__error { background: $log-error-bg; color: $log-error; }
  &__notice { background: $selection; color: $green; }
  &__empty { padding: 24px; color: $muted; font-size: 12px; }
}
@media (min-width: $knowledge-stack-width + 1px) {
  .knowledge-list {
    padding-top: 28px;
    &__heading { @include knowledge.desktop-header; gap: 14px; }
    &__description { font-size: 14px; }
    &__boundary { border: 0; border-top: 1px solid $knowledge-divider; padding: 12px 0 0; }
    &__primary { min-height: 44px; }
    &__card { @include knowledge.desktop-surface; }
    &__toolbar { padding: 15px 20px; }
    &__catalog-title h2 { font-size: 18px; }
    &__table {
      @include knowledge.desktop-table;
      th:first-child, td:first-child { padding-left: 20px; }
      th:nth-child(4), th:nth-child(5), td:nth-child(4), td:nth-child(5) { text-align: right; }
      th:nth-child(6), td:nth-child(6) { padding-left: 18px; }
    }
    &__item { position: relative; min-height: 38px; padding-left: 49px; }
    &__folder {
      @include knowledge.desktop-mark;
      &::before { width: 18px; height: 12px; border-radius: 2px; transform: translateY(2px); }
      &::after { content: ''; position: absolute; top: 11px; left: 10px; width: 8px; height: 4px; border: 1.5px solid currentColor; border-bottom: 0; border-radius: 2px 2px 0 0; }
    }
    &__name { text-decoration: none; &:hover, &:focus-visible { text-decoration: underline; } }
    &__updated { color: $log-muted; }
  }
}
@media (max-width: $knowledge-stack-width) {
  .knowledge-list {
    padding: 20px 16px 32px;
    &__table { @include knowledge.stacked-table; }
    &__table tbody tr { padding-top: 8px; }
    &__row-title, &__updated, &__row-actions { grid-column: 1 / -1; }
    &__name { display: flex; align-items: center; min-height: 44px; }
    &__row-title small { margin-top: 0; }
    &__row-actions--empty { display: none !important; }
    &__mobile-label { display: inline; margin-right: 6px; color: $log-muted; }
    &__fields { grid-template-columns: minmax(0, 1fr); }
    &__heading { align-items: flex-start; flex-wrap: wrap; }
    &__primary, &__toolbar button { min-height: 44px; }
    &__toolbar { padding: 10px 14px; border-bottom: 1px solid $line; }
  }
}
</style>
