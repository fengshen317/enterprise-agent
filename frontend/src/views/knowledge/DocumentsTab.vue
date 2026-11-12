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
import DocumentChunksDialog from './DocumentChunksDialog.vue'
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

async function uploadAll(): Promise<void> {
  if (uploading.value || !uploadItems.value.length) return
  uploading.value = true; error.value = ''; notice.value = ''
  for (const item of uploadItems.value) {
    if (item.status !== 'pending') continue
    item.status = 'uploading'
    try {
      await uploadKnowledgeDocument(props.kb.id, item.file)
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
      <div>
        <h2>文档管理</h2>
        <p class="documents-tab__hint">
          上传后自动写入解析配置并触发解析；可查看原文、分块和检索效果。
        </p>
      </div>
      <div class="documents-tab__upload">
        <input
          ref="fileInput"
          name="knowledgeFiles"
          type="file"
          multiple
          :disabled="uploading"
          @change="onFiles"
        /><button
          type="button"
          class="documents-tab__primary"
          :disabled="uploading || !uploadItems.length"
          @click="uploadAll"
        >
          {{ uploading ? '上传中…' : '上传所选文件' }}
        </button><button
          type="button"
          :disabled="loading"
          @click="load().then(schedulePoll)"
        >
          刷新状态
        </button>
      </div>
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
      class="documents-tab__table-wrap"
      :aria-busy="loading"
    >
      <table class="documents-tab__table">
        <thead><tr><th>文档</th><th>状态</th><th>Chunk</th><th>Token</th><th>操作</th></tr></thead>
        <tbody>
          <tr
            v-for="item in documents"
            :key="item.id"
          >
            <td>
              <strong>{{ item.name }}</strong><small>{{ item.mime }} · {{ formatSize(item.size) }} · {{ timeLabel(item.created_at) }}</small>
            </td>
            <td>
              <span class="documents-tab__state">{{ statusLabel(item.status) }}</span>
              <div
                v-if="ACTIVE.has(item.status)"
                class="documents-tab__progress"
                role="progressbar"
                :aria-valuenow="Math.round(item.progress * 100)"
              >
                <span :style="{ width: progressLabel(item.progress) }" />
              </div>
              <small v-if="item.error_message">{{ item.error_message }}</small>
              <small v-else-if="item.error_code">{{ errorLabel(item.error_code) }}</small>
            </td>
            <td>{{ item.chunk_count }}</td>
            <td>{{ item.token_count }}</td>
            <td class="documents-tab__row-actions">
              <button
                type="button"
                @click="preview(item)"
              >
                查看原文
              </button><button
                type="button"
                @click="chunkDoc = item"
              >
                查看分块
              </button><button
                type="button"
                :disabled="!!rowBusy"
                @click="reparseDoc = item"
              >
                重新解析
              </button><button
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
        v-if="!documents.length"
        class="documents-tab__empty"
      >
        {{ loading ? '正在读取文档…' : '还没有文档。支持 PDF、Word、Excel、PPT、TXT、Markdown、HTML 等。' }}
      </p>
    </div>
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
.documents-tab {
  background: white; border: 1px solid $line; border-radius: 8px; padding: 22px; margin-bottom: 22px;
  &__toolbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; flex-wrap: wrap; h2 { font-size: 17px; font-weight: 500; } }
  &__hint { color: $muted; font-size: 11px; line-height: 1.8; margin-top: 8px; max-width: 520px; }
  &__upload { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; input[type="file"] { max-width: 260px; font-size: 11px; } button { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 8px 12px; color: $green; font-size: 12px; } }
  &__primary { background: $green !important; color: white !important; border-color: $green !important; }
  &__queue { list-style: none; margin: 16px 0 0; padding: 12px 14px; background: $paper; border-radius: 6px; font-size: 11px; li { display: flex; flex-wrap: wrap; gap: 10px; padding: 4px 0; color: $green; small { color: #934b40; width: 100%; } } }
  &__table-wrap { overflow-x: auto; margin-top: 18px; }
  &__table { width: 100%; border-collapse: collapse; text-align: left; th { background: $paper; color: $muted; font-size: 11px; font-weight: 400; padding: 12px 16px; white-space: nowrap; } td { padding: 15px 16px; border-bottom: 1px solid $line; font-size: 12px; white-space: nowrap; vertical-align: top; } strong { font-weight: 500; } small { display: block; color: $muted; margin-top: 7px; max-width: 340px; white-space: normal; line-height: 1.7; } }
  &__state { display: inline-block; padding: 3px 7px; border-radius: 4px; background: #eef0eb; color: $muted; font-size: 10px; }
  &__progress { width: 120px; height: 4px; background: #e8ece3; border-radius: 4px; margin-top: 7px; overflow: hidden; span { display: block; height: 100%; background: $green; } }
  &__row-actions { display: flex; flex-wrap: wrap; gap: 6px; button { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 6px 9px; color: $green; font-size: 11px; } }
  &__error, &__notice { padding: 12px 16px; border-radius: 5px; font-size: 12px; line-height: 1.8; margin-top: 16px; }
  &__error { background: #faeee6; color: #934b40; }
  &__notice { background: #eaf0e1; color: $green; }
  &__empty { padding: 24px 0; color: $muted; font-size: 12px; }
}
@media (max-width: 900px) { .documents-tab { padding: 16px; &__table td, &__table th { padding: 12px 10px; } } }
</style>
