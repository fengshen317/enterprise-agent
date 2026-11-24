<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  deleteKnowledgeDocument,
  documentFileUrl,
  listKnowledgeDocuments,
  uploadKnowledgeDocument,
  type KnowledgeBase,
  type KnowledgeDocument,
} from '../../api/knowledge'
import { useAuthStore } from '../../stores/auth'
import DocumentChunksDialog from './DocumentChunksDialog.vue'
import DocumentReadersDialog from './DocumentReadersDialog.vue'
import ReparseDialog from './ReparseDialog.vue'
import {
  errorLabel,
  formatSize,
  progressLabel,
  statusLabel,
  timeLabel,
} from './knowledgeUi'

interface UploadItem {
  file: File
  status: 'pending' | 'uploading' | 'done' | 'failed'
  message?: string
}

const props = defineProps<{ kb: KnowledgeBase }>()
const emit = defineEmits<{ changed: [] }>()

const documents = ref<KnowledgeDocument[]>([])
const loading = ref(false)
const uploading = ref(false)
const error = ref('')
const notice = ref('')
const rowBusy = ref('')
const confirmDelete = ref('')
const uploadItems = ref<UploadItem[]>([])
const chunkDoc = ref<KnowledgeDocument | null>(null)
const reparseDoc = ref<KnowledgeDocument | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const auth = useAuthStore()
const uploadMode = ref<'inherited' | 'restricted'>('inherited')
const openingUploadReaders = ref(false)
const editingReaders = ref<KnowledgeDocument>()
const currentUserId = computed(() => auth.session?.user.id ?? '')
let alive = true
let revision = 0
let timer: ReturnType<typeof setTimeout> | undefined
const POLL_MS = 5000
const ACTIVE = new Set(['uploading', 'uploaded', 'configuring', 'parsing'])

const hasActive = computed(() => documents.value.some(item => ACTIVE.has(item.status)))

function schedulePoll(): void {
  clearTimeout(timer)
  if (!hasActive.value || !alive) return
  timer = setTimeout(() => { void load().then(schedulePoll) }, POLL_MS)
}

function clearSelection(): void {
  uploadItems.value = []
  if (fileInput.value) fileInput.value.value = ''
}

function handleError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(): Promise<void> {
  const token = ++revision
  loading.value = true; error.value = ''
  try {
    const rows = await listKnowledgeDocuments(props.kb.id)
    if (alive && token === revision) documents.value = rows
  } catch (value) { if (alive && token === revision) handleError(value) }
  finally { if (alive && token === revision) loading.value = false }
}

function onFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  uploadItems.value = files.map(file => ({ file, status: 'pending' }))
}

function startUpload(): void {
  if (props.kb.visibility === 'workspace' && uploadMode.value === 'restricted') openingUploadReaders.value = true
  else void uploadAll()
}

async function uploadAll(readers?: string[]): Promise<void> {
  if (uploading.value || !uploadItems.value.length) return
  uploading.value = true; error.value = ''; notice.value = ''
  for (const item of uploadItems.value) {
    if (item.status !== 'pending') continue
    item.status = 'uploading'
    try {
      if (readers) await uploadKnowledgeDocument(props.kb.id, item.file, readers)
      else await uploadKnowledgeDocument(props.kb.id, item.file)
      item.status = 'done'
    } catch (value) {
      item.status = 'failed'
      item.message = value instanceof Error ? value.message : '上传失败'
    }
  }
  uploading.value = false
  if (!alive) return
  const failed = uploadItems.value.filter(item => item.status === 'failed').length
  notice.value = failed ? `上传结束，${failed} 个文件失败。` : '全部文件已上传并触发解析。'
  clearSelection()
  await load()
  emit('changed')
  schedulePoll()
}

function uploadRestricted(readers: string[]): void {
  openingUploadReaders.value = false
  void uploadAll(readers)
}

async function afterReadersChanged(): Promise<void> {
  editingReaders.value = undefined
  notice.value = '读取权限已更新。'
  await load()
  emit('changed')
}

function preview(item: KnowledgeDocument): void {
  window.open(documentFileUrl(item.id), '_blank', 'noopener')
}

async function remove(item: KnowledgeDocument): Promise<void> {
  if (rowBusy.value) return
  if (confirmDelete.value !== item.id) { confirmDelete.value = item.id; return }
  rowBusy.value = item.id; error.value = ''; notice.value = ''
  try {
    await deleteKnowledgeDocument(item.id)
    if (!alive) return
    notice.value = '文档已删除。'
    confirmDelete.value = ''
    await load()
    emit('changed')
  } catch (value) { if (alive) handleError(value) }
  finally { rowBusy.value = '' }
}

function resetDeleteConfirmation(event: Event, itemId: string): void {
  if (!(event.currentTarget as HTMLDetailsElement).open && confirmDelete.value === itemId) confirmDelete.value = ''
}

async function afterReparse(): Promise<void> {
  reparseDoc.value = null
  notice.value = '已重新触发解析。'
  await load()
  emit('changed')
  schedulePoll()
}

watch(() => props.kb.id, () => { void load().then(schedulePoll) }, { immediate: true })
onBeforeUnmount(() => { alive = false; revision++; clearTimeout(timer) })
</script>

<template>
  <section class="documents-tab">
    <div class="documents-tab__toolbar">
      <h2>文档管理</h2>
      <button
        class="documents-tab__refresh"
        type="button"
        :disabled="loading"
        @click="load().then(schedulePoll)"
      >
        刷新状态
      </button>
    </div>
    <p
      v-if="error"
      class="documents-tab__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="documents-tab__notice"
      role="status"
    >
      {{ notice }}
    </p>
    <ul
      v-if="uploadItems.length"
      class="documents-tab__queue"
    >
      <li
        v-for="item in uploadItems"
        :key="item.file.name"
      >
        <span>{{ item.file.name }}</span><span>{{ item.status === 'pending' ? '待上传' : item.status === 'uploading' ? '上传中…' : item.status === 'done' ? '已上传' : '失败' }}</span><small v-if="item.message">{{ item.message }}</small>
      </li>
    </ul>
    <div
      class="documents-tab__body"
      :class="{ 'documents-tab__body--manager': kb.can_manage }"
    >
      <section
        v-if="kb.can_manage"
        class="documents-tab__upload"
        aria-label="添加文档"
      >
        <div class="documents-tab__upload-heading">
          <h3>添加文档</h3>
          <p class="documents-tab__hint">
            上传后按知识库配置自动解析。
          </p>
        </div>
        <label v-if="kb.visibility === 'workspace'">
          读取权限
          <select
            v-model="uploadMode"
            :disabled="uploading"
          >
            <option value="inherited">继承知识库</option>
            <option value="restricted">仅指定成员</option>
          </select>
        </label>
        <input
          ref="fileInput"
          aria-label="选择要上传的文档"
          name="knowledgeFiles"
          type="file"
          multiple
          :disabled="uploading"
          @change="onFiles"
        />
        <button
          type="button"
          class="documents-tab__primary"
          :disabled="uploading || !uploadItems.length"
          @click="startUpload"
        >
          {{ uploading ? '上传中…' : kb.visibility === 'workspace' && uploadMode === 'restricted' ? '选择读者并上传' : '上传所选文件' }}
        </button>
      </section>
      <div
        class="documents-tab__table-wrap"
        :aria-busy="loading"
      >
        <table
          class="documents-tab__table"
          aria-label="文档"
          role="table"
        >
          <thead role="rowgroup">
            <tr role="row">
              <th scope="col">
                文档
              </th><th scope="col">
                状态
              </th><th scope="col">
                分块
              </th><th scope="col">
                Token
              </th><th scope="col">
                操作
              </th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            <tr
              v-for="item in documents"
              :key="item.id"
              role="row"
            >
              <td
                class="documents-tab__row-title"
                role="cell"
              >
                <div class="documents-tab__file">
                  <span
                    class="documents-tab__file-mark"
                    aria-hidden="true"
                  />
                  <strong class="documents-tab__name">{{ item.name }}</strong>
                  <span
                    v-if="item.restricted"
                    class="documents-tab__scope"
                  >仅指定成员</span>
                  <small>{{ item.mime }} · {{ formatSize(item.size) }} · {{ timeLabel(item.created_at) }}</small>
                </div>
              </td>
              <td
                class="documents-tab__row-status"
                role="cell"
              >
                <span
                  class="documents-tab__state"
                  :data-status="item.status"
                >{{ statusLabel(item.status) }}</span>
                <div
                  v-if="ACTIVE.has(item.status)"
                  class="documents-tab__progress"
                  role="progressbar"
                  :aria-label="item.name + '解析进度'"
                  :aria-valuenow="Math.round(item.progress * 100)"
                >
                  <span :style="{ width: progressLabel(item.progress) }" />
                </div>
                <small v-if="item.error_message">{{ item.error_message }}</small>
                <small v-else-if="item.error_code">{{ errorLabel(item.error_code) }}</small>
              </td>
              <td
                class="documents-tab__count"
                role="cell"
              >
                <span
                  class="documents-tab__mobile-label"
                  aria-hidden="true"
                >分块</span>{{ item.chunk_count }}
              </td>
              <td
                class="documents-tab__count"
                role="cell"
              >
                <span
                  class="documents-tab__mobile-label"
                  aria-hidden="true"
                >Token</span>{{ item.token_count }}
              </td>
              <td
                class="documents-tab__row-actions"
                role="cell"
              >
                <div class="documents-tab__actions">
                  <button
                    type="button"
                    @click="preview(item)"
                  >
                    查看原文
                  </button>
                  <button
                    type="button"
                    @click="chunkDoc = item"
                  >
                    查看分块
                  </button>
                  <button
                    v-if="kb.can_manage && item.restricted"
                    type="button"
                    @click="editingReaders = item"
                  >
                    读取权限
                  </button>
                  <details
                    v-if="kb.can_manage"
                    class="documents-tab__more"
                    @toggle="resetDeleteConfirmation($event, item.id)"
                  >
                    <summary :aria-label="'更多操作：' + item.name">
                      更多
                    </summary>
                    <div class="documents-tab__more-actions">
                      <button
                        type="button"
                        :disabled="!!rowBusy"
                        @click="reparseDoc = item"
                      >
                        重新解析
                      </button>
                      <button
                        class="documents-tab__delete"
                        type="button"
                        :disabled="!!rowBusy"
                        @click="remove(item)"
                      >
                        {{ confirmDelete === item.id ? '确认删除' : '删除' }}
                      </button>
                    </div>
                  </details>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <p
          v-if="!documents.length"
          class="documents-tab__empty"
        >
          {{ loading ? '正在读取文档…' : '还没有文档。支持 PDF、Word、Excel、PPT、TXT、Markdown、HTML 等。' }}
        </p>
      </div>
    </div>
    <DocumentReadersDialog
      v-if="openingUploadReaders || editingReaders"
      :kb-id="kb.id"
      :document="editingReaders"
      :current-user-id="currentUserId"
      @close="openingUploadReaders = false; editingReaders = undefined"
      @selected="uploadRestricted"
      @saved="afterReadersChanged"
    />
    <DocumentChunksDialog
      v-if="chunkDoc"
      :document="chunkDoc"
      @close="chunkDoc = null"
    />
    <ReparseDialog
      v-if="reparseDoc"
      :kb="kb"
      :document="reparseDoc"
      @close="reparseDoc = null"
      @done="afterReparse"
    />
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
@use '../../styles/knowledge';
.documents-tab {
  background: $surface; border: 1px solid $line; border-radius: 8px; padding: 22px; margin-bottom: 22px;
  &__toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; h2 { font-size: 18px; font-weight: 600; } }
  &__refresh { @include knowledge.row-action; }
  &__body, &__table-wrap { min-width: 0; }
  &__hint { color: $log-muted; font-size: 13px; line-height: 1.65; }
  &__upload {
    display: flex; min-width: 0; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px;
    input[type="file"] { width: min(100%, 235px); font-size: 12px; }
    input::file-selector-button { margin-right: 6px; @include knowledge.row-action; }
    button { @include knowledge.row-action; }
    .documents-tab__primary { background: $green; color: $on-green; border-color: $green; &:hover { background: $ink; } }
    label { color: $log-muted; font-size: 12px; }
    select { @include knowledge.row-action; }
  }
  &__upload-heading { flex-basis: 100%; h3 { font-size: 15px; margin-bottom: 5px; } }
  &__file-mark { display: none; }
  &__scope { display: inline-block; margin-top: 6px; color: $log-warning; background: $log-warning-bg; padding: 3px 7px; border-radius: 5px; font-size: 12px; }
  &__queue { list-style: none; margin: 16px 0 0; padding: 12px 14px; background: $paper; border-radius: 6px; font-size: 11px; li { display: flex; flex-wrap: wrap; gap: 10px; padding: 4px 0; color: $green; small { color: $log-error; width: 100%; } } }
  &__table-wrap { margin-top: 18px; }
  &__table {
    @include knowledge.table(13px 12px);
    th:nth-child(1) { width: 35%; }
    th:nth-child(2) { width: 17%; }
    th:nth-child(3), th:nth-child(4) { width: 9%; }
    th:nth-child(5) { width: 30%; }
  }
  &__name { display: block; color: $ink; font-size: 15px; font-weight: 600; }
  &__state { @include knowledge.state; }
  &__progress { width: min(100%, 120px); height: 4px; background: $line; border-radius: 4px; margin-top: 7px; overflow: hidden; span { display: block; height: 100%; background: $green; } }
  &__count { color: $log-muted; font-variant-numeric: tabular-nums; }
  &__mobile-label { display: none; }
  &__actions, &__more-actions { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 6px; }
  &__actions button { @include knowledge.row-action; }
  &__more { @include knowledge.disclosure; min-width: 0; }
  &__more-actions { margin-top: 6px; }
  &__actions &__delete { color: $log-error; }
  &__error, &__notice { padding: 12px 16px; border-radius: 5px; font-size: 12px; line-height: 1.8; margin-top: 16px; }
  &__error { background: $log-error-bg; color: $log-error; }
  &__notice { background: $selection; color: $green; }
  &__empty { padding: 24px 0; color: $muted; font-size: 12px; }
}
@media (min-width: $knowledge-stack-width + 1px) {
  .documents-tab {
    background: none;
    border: 0;
    padding: 0;
    &__toolbar { margin-bottom: 12px; h2 { font-size: 21px; } }
    &__upload {
      padding: 16px 18px;
      margin: 0 0 14px;
      border: 1px solid $knowledge-divider;
      border-radius: 10px;
      background: $knowledge-tint;
    }
    &__table-wrap { @include knowledge.desktop-surface; margin-top: 0; }
    &__table {
      @include knowledge.desktop-table;
      th:first-child, td:first-child { padding-left: 16px; }
      th:nth-child(3), th:nth-child(4), td:nth-child(3), td:nth-child(4) { text-align: right; }
      th:nth-child(5), td:nth-child(5) { padding-left: 16px; }
      th:first-child { border-top-left-radius: 11px; }
      th:last-child { border-top-right-radius: 11px; }
    }
    &__file { position: relative; min-height: 38px; padding-left: 46px; }
    &__file-mark {
      @include knowledge.desktop-mark;
      width: 35px;
      &::before { width: 14px; height: 18px; border-radius: 2px; }
      &::after { content: ''; width: 6px; height: 5px; border-block: 1px solid currentColor; position: absolute; }
    }
    &__empty { padding-inline: 16px; }
  }
}
@media (min-width: 1240px) {
  .documents-tab {
    &__body--manager {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 226px;
      grid-template-areas: 'table upload';
      align-items: start;
      gap: 16px;
    }
    &__upload {
      grid-area: upload;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 12px;
      margin: 0;
      label { display: grid; gap: 4px; }
      select, input[type="file"] { width: 100%; }
      .documents-tab__primary { justify-content: center; min-height: 42px; }
    }
    &__body--manager &__table-wrap { grid-area: table; }
    &__body--manager &__table th:nth-child(1) { width: 37%; }
    &__body--manager &__table th:nth-child(2) { width: 15%; }
  }
}
@media (max-width: $knowledge-stack-width) {
  .documents-tab {
    padding: 14px;
    &__table { @include knowledge.stacked-table; }
    &__table tbody tr { padding-inline: 0; }
    &__table tbody tr:first-child { border-top: 1px solid $line; }
    &__row-title, &__row-status, &__row-actions { grid-column: 1 / -1; }
    &__mobile-label { display: inline; margin-right: 6px; }
    &__actions, &__more-actions { gap: 8px; }
  }
}
</style>
