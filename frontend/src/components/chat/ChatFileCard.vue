<script setup lang="ts">
import { attachmentSizeLabel } from '../../api/chatAttachments'

defineProps<{ filename: string; size: number; preview?: string; download?: string; note?: string; showDownloadAction?: boolean }>()
</script>

<template>
  <div
    class="chat-file"
    :class="{ 'chat-file--with-download': showDownloadAction }"
  >
    <a
      v-if="preview && download"
      class="chat-file__preview"
      :href="preview"
      :aria-label="'查看图片 ' + filename"
      target="_blank"
      rel="noopener noreferrer"
    ><img
      :src="preview"
      alt=""
      loading="lazy"
    /></a>
    <span
      v-else-if="preview"
      class="chat-file__preview"
    ><img
      :src="preview"
      alt=""
    /></span>
    <span
      v-else
      class="chat-file__preview chat-file__preview--document"
      aria-hidden="true"
    >▤</span>
    <div class="chat-file__details">
      <a
        v-if="download && !showDownloadAction"
        class="chat-file__name"
        :href="download"
        :title="filename"
        :aria-label="'下载 ' + filename"
        target="_blank"
        rel="noopener noreferrer"
      >{{ filename }}</a>
      <span
        v-else
        class="chat-file__name"
        :title="filename"
      >{{ filename }}</span>
      <small>{{ attachmentSizeLabel(size) }}<template v-if="note"> · {{ note }}</template></small>
    </div>
    <div class="chat-file__actions">
      <a
        v-if="download && showDownloadAction"
        class="chat-file__download"
        :href="download"
        :aria-label="'下载 ' + filename"
        target="_blank"
        rel="noopener noreferrer"
      >下载</a>
      <slot />
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.chat-file {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
  max-width: 100%;
  padding: 6px 8px;
  border: 1px solid $line;
  border-radius: 8px;
  background: $surface;

  &__preview {
    display: grid; place-items: center; flex: 0 0 40px; height: 38px; overflow: hidden;
    background: $selection; border-radius: 5px; text-decoration: none;
    img { width: 100%; height: 100%; object-fit: cover; }
    &--document { font-size: 18px; color: $green; }
  }
  &__details { display: grid; gap: 2px; min-width: 0; }
  &__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: $ink; }
  small { color: $muted; font-size: 11px; }
  &__actions:empty { display: none; }
  &__actions { margin-left: auto; flex: none; }
  &__actions :deep(button) {
    padding: 4px 7px; border: 1px solid $line; border-radius: 5px; color: $green;
    background: $surface; font-size: 11px; cursor: pointer;
    &:hover:not(:disabled) { background: $paper; }
    &:disabled { opacity: .5; cursor: default; }
  }

  &--with-download { flex-wrap: wrap; }
  &--with-download &__details { flex: 1 1 8rem; }
  &--with-download &__name { overflow-wrap: anywhere; white-space: normal; }
  &--with-download &__actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px 8px;
    max-width: 100%;
    font-size: 12px;

    :deep(a:not(.chat-file__download)) { color: $green; }
  }
  &__download {
    padding: 4px 9px;
    border-radius: $control-radius;
    background: $selection;
    color: $green;
    font-weight: 600;
    text-decoration: none;

    &:hover { background: $sidebar; text-decoration: underline; }
    &:focus-visible { outline: 2px solid $green; outline-offset: 2px; }
  }
}
</style>
