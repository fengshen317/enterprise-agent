<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  getKnowledgeDocumentReaders,
  listKnowledgeAccessMembers,
  maxDocumentReaders,
  setKnowledgeDocumentReaders,
  type KnowledgeAccessMember,
  type KnowledgeDocument,
} from '../../api/knowledge'

const props = defineProps<{ kbId: string; document?: KnowledgeDocument; currentUserId: string }>()
const emit = defineEmits<{ close: []; selected: [readers: string[]]; saved: [] }>()
const isUpload = computed(() => !props.document)
const selected = ref<string[]>([])
const names = ref<Record<string, string>>({})
const page = ref<KnowledgeAccessMember[]>([])
const search = ref('')
const activeSearch = ref('')
const searchInput = ref<HTMLInputElement | null>(null)
const offset = ref(0)
const error = ref('')
const loading = ref(false)
const initialized = ref(false)
const saving = ref(false)
const pageSize = 20
let request = 0
let alive = true

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '无法更新文档权限，请重试。'
}

async function load(start = 0): Promise<void> {
  const token = ++request
  loading.value = true
  error.value = ''
  try {
    const rows = await listKnowledgeAccessMembers(props.kbId, activeSearch.value, start, pageSize)
    if (!alive || token !== request) return
    rows.forEach(row => { names.value[row.id] = row.display_name + ' · ' + row.username })
    page.value = rows
    offset.value = start
  } catch (value) {
    if (alive && token === request) showError(value)
  } finally {
    if (alive && token === request) loading.value = false
  }
}

async function initialize(): Promise<void> {
  try {
    const users = props.document
      ? await getKnowledgeDocumentReaders(props.document.id)
      : [props.currentUserId]
    if (!alive) return
    selected.value = users
    initialized.value = true
    await load()
    await nextTick()
    if (alive) searchInput.value?.focus()
  } catch (value) {
    if (alive) showError(value)
  }
}

function searchMembers(): void {
  activeSearch.value = search.value.trim()
  void load()
}

function toggle(id: string): void {
  if (isUpload.value && id === props.currentUserId) return
  if (selected.value.includes(id)) selected.value = selected.value.filter(existing => existing !== id)
  else if (selected.value.length < maxDocumentReaders) selected.value = [...selected.value, id]
}

async function submit(): Promise<void> {
  if (!initialized.value || saving.value || loading.value) return
  if (!props.document) {
    emit('selected', selected.value)
    return
  }
  saving.value = true
  error.value = ''
  try {
    await setKnowledgeDocumentReaders(props.document.id, selected.value)
    if (alive) emit('saved')
  } catch (value) {
    if (alive) showError(value)
  } finally { saving.value = false }
}

onMounted(() => { void initialize() })
onBeforeUnmount(() => { alive = false; request++ })
</script>

<template>
  <div
    class="document-readers"
    role="dialog"
    aria-modal="true"
    aria-label="文档读取权限"
    @keydown.esc="!saving && emit('close')"
  >
    <section class="document-readers__panel">
      <header>
        <h2>{{ document ? '读取权限 · ' + document.name : '选择受限文件的读者' }}</h2>
        <p v-if="isUpload">
          只有所选成员可读取文件。上传时包含当前账号。
        </p>
        <p v-else>
          名单留空会关闭文件内容访问。移除自己的授权后，文件会从你的列表隐藏。
        </p>
      </header>
      <p
        v-if="error"
        class="document-readers__error"
        role="alert"
      >
        {{ error }}
      </p>
      <template v-if="initialized">
        <form
          class="document-readers__search"
          @submit.prevent="searchMembers"
        >
          <input
            ref="searchInput"
            v-model="search"
            type="search"
            maxlength="100"
            aria-label="搜索成员姓名或账号"
            placeholder="搜索成员姓名或账号"
          />
          <button :disabled="saving">
            搜索
          </button>
        </form>
        <ul
          v-if="selected.length"
          class="document-readers__selected"
          aria-label="已选择的成员"
        >
          <li
            v-for="id in selected"
            :key="id"
          >
            <button
              type="button"
              :disabled="saving || (isUpload && id === currentUserId)"
              :aria-label="'移除 ' + (names[id] ?? id)"
              @click="toggle(id)"
            >
              {{ id === currentUserId ? '当前账号' : names[id] ?? id }} {{ isUpload && id === currentUserId ? '' : '×' }}
            </button>
          </li>
        </ul>
        <p
          v-if="loading"
          role="status"
        >
          正在读取成员…
        </p>
        <p
          v-else-if="!page.length"
        >
          没有符合条件的成员。
        </p>
        <ul
          v-else
          class="document-readers__members"
        >
          <li
            v-for="member in page"
            :key="member.id"
          >
            <label>
              <input
                type="checkbox"
                :checked="selected.includes(member.id)"
                :disabled="saving || (isUpload && member.id === currentUserId)
                  || (!selected.includes(member.id) && selected.length >= maxDocumentReaders)"
                @change="toggle(member.id)"
              />
              <span>{{ member.display_name }} <small>{{ member.username }}</small></span>
            </label>
          </li>
        </ul>
        <div class="document-readers__actions">
          <button
            type="button"
            :disabled="saving || loading || offset === 0"
            @click="load(offset - pageSize)"
          >
            上一页
          </button>
          <button
            type="button"
            :disabled="saving || loading || page.length < pageSize"
            @click="load(offset + pageSize)"
          >
            下一页
          </button>
          <small>已选 {{ selected.length }} / {{ maxDocumentReaders }} 人</small>
        </div>
      </template>
      <footer class="document-readers__actions">
        <button
          type="button"
          :disabled="saving"
          @click="emit('close')"
        >
          取消
        </button>
        <button
          type="button"
          class="document-readers__primary"
          :disabled="!initialized || saving || loading"
          @click="submit"
        >
          {{ saving ? '保存中…' : isUpload ? '确认并上传' : '保存权限' }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.document-readers {
  position: fixed;
  inset: 0;
  z-index: 30;
  display: grid;
  place-items: center;
  padding: 16px;
  background: rgba(28, 36, 26, 0.38);

  &__panel {
    width: min(100%, 560px);
    max-height: 90vh;
    overflow: auto;
    padding: 24px;
    background: $surface;
    border: 1px solid $line;
    border-radius: 8px;
  }
  h2 { font-size: 17px; overflow-wrap: anywhere; }
  p, small { color: $muted; font-size: 12px; }
  button { border: 1px solid $line; border-radius: 5px; padding: 7px 10px; background: $paper; color: $green; }
  button:disabled { opacity: .55; }
  &__search, &__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 12px 0; }
  &__search input { flex: 1; min-width: 12rem; padding: 7px 10px; border: 1px solid $line; border-radius: 5px; }
  &__members, &__selected { list-style: none; padding: 0; margin: 12px 0; }
  &__members {
    max-height: 220px;
    overflow: auto;
    label { display: flex; gap: 8px; align-items: center; padding: 8px 0; border-bottom: 1px solid $line; }
    small { margin-left: 4px; }
  }
  &__selected { display: flex; flex-wrap: wrap; gap: 6px; button { overflow-wrap: anywhere; } }
  &__panel footer { justify-content: flex-end; margin-bottom: 0; }
  &__primary { background: $green !important; color: $on-green !important; }
  &__error { color: $log-error !important; }
}
</style>
