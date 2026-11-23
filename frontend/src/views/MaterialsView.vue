<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { attachmentAccept, attachmentSizeLabel, isAttachmentImage, maxAttachmentBytes, maxAttachmentCount, maxTotalAttachmentBytes } from '../api/chatAttachments'
import { getMaterial, listMaterials, materialFileUrl, uploadMaterials, type Material } from '../api/materials'
import { useAuthStore } from '../stores/auth'

const pageSize = 20
const route = useRoute()
const auth = useAuthStore()
const filePicker = ref<HTMLInputElement>()
const input = ref('')
const query = ref('')
const items = ref<Material[]>([])
const selected = ref<Material | null>(null)
const page = ref(0)
const hasMore = ref(false)
const loading = ref(false)
const uploading = ref(false)
const error = ref('')
const notice = ref('')
const detailError = ref('')
let alive = true
let revision = 0
const selectedId = computed(() => typeof route.params.materialId === 'string' ? route.params.materialId : '')

function message(value: unknown): string {
  return value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(target = page.value): Promise<void> {
  const token = ++revision
  loading.value = true; error.value = ''
  try {
    const result = await listMaterials(query.value, target * pageSize, pageSize)
    if (!alive || token !== revision) return
    items.value = result.items
    hasMore.value = result.has_more
    page.value = target
  } catch (value) { if (alive && token === revision) error.value = message(value) }
  finally { if (alive && token === revision) loading.value = false }
}

async function search(): Promise<void> {
  query.value = input.value.trim()
  notice.value = ''
  await load(0)
}

async function upload(event: Event): Promise<void> {
  const picker = event.target as HTMLInputElement
  const files = Array.from(picker.files ?? [])
  picker.value = ''
  if (!files.length || uploading.value) return
  error.value = ''; notice.value = ''
  if (files.length > maxAttachmentCount || files.some(file => !file.size || file.size > maxAttachmentBytes)
    || files.reduce((size, file) => size + file.size, 0) > maxTotalAttachmentBytes) {
    error.value = '每次最多上传 4 个非空文件，单个最多 8 MB，合计最多 16 MB。'
    return
  }
  uploading.value = true
  try {
    const result = await uploadMaterials(files)
    if (!alive) return
    input.value = ''; query.value = ''
    notice.value = `已归档 ${result.length} 份材料。`
    await load(0)
  } catch (value) { if (alive) error.value = message(value) }
  finally { uploading.value = false }
}

function status(item: Material): string {
  if (isAttachmentImage(item.media_type)) return '图片 · 可按文件名查找'
  if (!item.text_available) return '未提取到文字'
  return item.text_truncated ? '已提取部分文字' : '正文可检索'
}

watch(selectedId, async (id) => {
  selected.value = null; detailError.value = ''
  if (!id) return
  try {
    const value = await getMaterial(id)
    if (alive && selectedId.value === id) selected.value = value
  } catch (value) { if (alive && selectedId.value === id) detailError.value = message(value) }
}, { immediate: true })
void load(0)
onBeforeUnmount(() => { alive = false })
</script>

<template>
  <section class="materials">
    <header class="materials__heading">
      <div>
        <h1>我的材料</h1>
        <p>聊天附件和助手生成的文件自动归档，也可直接上传。你的 Agent 可以在当前工作空间检索这些材料。</p>
      </div>
      <input
        ref="filePicker"
        type="file"
        :accept="attachmentAccept"
        multiple
        hidden
        @change="upload"
      />
      <button
        type="button"
        class="materials__primary"
        :disabled="uploading || auth.activeWorkspace?.status === 'archived'"
        @click="filePicker?.click()"
      >
        {{ uploading ? '正在上传…' : '＋ 上传材料' }}
      </button>
    </header>
    <p
      v-if="notice"
      class="materials__notice"
      role="status"
    >
      {{ notice }}
    </p>
    <p
      v-if="error || detailError"
      class="materials__error"
      role="alert"
    >
      {{ error || detailError }}
    </p>
    <section
      v-if="selected"
      class="materials__detail"
      aria-label="所选材料"
    >
      <img
        v-if="isAttachmentImage(selected.media_type)"
        :src="materialFileUrl(selected.id)"
        :alt="selected.filename"
      />
      <div class="materials__info">
        <h2>{{ selected.filename }}</h2>
        <p>{{ attachmentSizeLabel(selected.size_bytes) }} · {{ status(selected) }}</p>
        <details
          v-if="selected.excerpt"
          class="materials__preview"
        >
          <summary>正文预览</summary>
          <pre>{{ selected.excerpt }}</pre>
        </details>
        <div class="materials__links">
          <a :href="materialFileUrl(selected.id, true)">下载原件</a>
          <RouterLink :to="{ path: '/chat', query: { material: selected.id } }">附到新对话 ↗</RouterLink>
          <RouterLink
            v-if="selected.source_conversation_id"
            :to="'/chat/' + selected.source_conversation_id"
          >来源对话</RouterLink>
          <RouterLink to="/materials">关闭详情</RouterLink>
        </div>
      </div>
    </section>
    <div
      class="materials__library"
      :aria-busy="loading"
    >
      <form
        class="materials__search"
        @submit.prevent="search"
      >
        <input
          v-model="input"
          type="search"
          aria-label="检索材料"
          placeholder="搜索文件名或正文"
          maxlength="120"
        />
        <button type="submit">
          搜索
        </button>
      </form>
      <p
        v-if="!items.length"
        class="materials__empty"
      >
        {{ loading ? '正在读取材料…' : query ? '没有找到匹配的材料。' : '还没有材料。在对话中发送附件或直接上传文件。' }}
      </p>
      <ul
        v-else
        class="materials__list"
      >
        <li
          v-for="item in items"
          :key="item.id"
          class="materials__row"
        >
          <span
            class="materials__file-icon"
            aria-hidden="true"
          >{{ isAttachmentImage(item.media_type) ? '▧' : '▤' }}</span>
          <div class="materials__info">
            <RouterLink
              class="materials__filename"
              :to="'/materials/' + item.id"
            >{{ item.filename }}</RouterLink>
            <p>{{ attachmentSizeLabel(item.size_bytes) }} · {{ status(item) }}</p>
            <p
              v-if="item.excerpt"
              class="materials__excerpt"
            >
              {{ item.excerpt }}
            </p>
          </div>
          <time :datetime="item.created_at">{{ new Date(item.created_at).toLocaleDateString('zh-CN') }}</time>
        </li>
      </ul>
      <nav
        v-if="page || hasMore"
        class="materials__pagination"
        aria-label="材料分页"
      >
        <button
          type="button"
          :disabled="!page || loading"
          @click="load(page - 1)"
        >
          上一页
        </button>
        <span>第 {{ page + 1 }} 页</span>
        <button
          type="button"
          :disabled="!hasMore || loading"
          @click="load(page + 1)"
        >
          下一页
        </button>
      </nav>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.materials {
  max-width: 1180px; margin: 0 auto; padding: 36px clamp(16px, 4vw, 54px) 50px;
  &__heading, &__search, &__pagination, &__row, &__detail, &__links { display: flex; align-items: center; gap: 12px; }
  &__heading { justify-content: space-between; margin-bottom: 24px; }
  h1 { margin: 0; font-size: clamp(26px, 4vw, 39px); letter-spacing: -.04em; }
  h2 { margin: 0; overflow-wrap: anywhere; font-size: 17px; }
  p { color: $muted; }
  &__heading p { margin: 7px 0 0; line-height: 1.65; font-size: 14px; }
  &__library, &__detail { border: 1px solid $line; border-radius: 10px; background: white; }
  &__detail { padding: 18px; margin-bottom: 14px; align-items: flex-start; }
  &__detail img { max-height: 145px; max-width: min(42%, 180px); object-fit: contain; border-radius: 6px; }
  &__search { padding: 13px 16px; border-bottom: 1px solid $line; }
  &__search input { width: min(100%, 410px); min-width: 0; border: 1px solid $line; padding: 9px 11px; border-radius: 6px; font: inherit; font-size: 13px; color: $ink; }
  button { background: $paper; border: 1px solid $line; padding: 8px 11px; border-radius: 6px; color: $green; white-space: nowrap; font: inherit; font-size: 13px; }
  button:disabled { opacity: .55; }
  button.materials__primary { background: $green; border-color: $green; color: white; min-height: 38px; }
  &__list { margin: 0; padding: 0; list-style: none; }
  &__row { padding: 13px 16px; }
  &__row + &__row { border-top: 1px solid $line; }
  &__row time { margin-left: auto; color: $muted; font-size: 12px; white-space: nowrap; }
  &__file-icon { display: grid; place-items: center; flex: 0 0 38px; height: 38px; border-radius: 7px; background: $paper; color: $green; }
  &__info { min-width: 0; }
  &__info p { margin: 4px 0 0; font-size: 12px; }
  &__filename { color: $ink; overflow-wrap: anywhere; font-weight: 650; text-decoration: none; }
  &__filename:hover { text-decoration: underline; }
  &__excerpt { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
  &__preview { margin-top: 10px; font-size: 13px; }
  &__preview pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 22rem; overflow: auto; }
  &__links { flex-wrap: wrap; margin-top: 10px; font-size: 13px; }
  &__pagination { justify-content: end; padding: 12px 16px; border-top: 1px solid $line; font-size: 13px; }
  &__empty, &__error, &__notice { padding: 12px 16px; font-size: 13px; }
  p.materials__error { color: #934b40; background: #faeee6; }
  p.materials__notice { color: $green; background: #eaf0e1; }
  @media (max-width: 600px) { &__heading { align-items: flex-start; flex-direction: column; } &__row { flex-wrap: wrap; } }
}
</style>
