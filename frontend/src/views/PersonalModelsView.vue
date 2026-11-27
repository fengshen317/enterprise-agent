<script setup lang="ts">
import ModelProfilesPanel from './ModelProfilesPanel.vue'
</script>

<template>
  <section class="personal-models">
    <header class="personal-models__heading">
      <h1>模型配置</h1>
      <p>使用自己的模型，也可以沿用空间提供的模型。</p>
    </header>
    <ModelProfilesPanel
      personal
      :disabled="false"
      :revision="0"
    />
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.personal-models {
  max-width: 900px; margin: 0 auto; padding: 30px 24px;
  &__heading { margin-bottom: 24px; }
  h1 { margin: 0 0 8px; font-size: 28px; color: $ink; }
  p { margin: 0; color: $muted; font-size: 14px; }
  @media(max-width: 520px) { padding: 22px 16px; }
}
</style>
