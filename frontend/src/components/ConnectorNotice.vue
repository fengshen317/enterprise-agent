<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import { useConnectorsStore } from '../stores/connectors'
import { useAuthStore } from '../stores/auth'
import { connectorHealthLabel, connectorActionLabel } from '../api/connectors'

const connectors = useConnectorsStore()
const auth = useAuthStore()
const isAdmin = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
onMounted(connectors.acquire)
onUnmounted(connectors.release)
</script>

<template>
  <aside
    v-if="connectors.attention.length"
    class="connector-notice"
    aria-label="连接器待处理提醒"
    aria-live="polite"
  >
    <div
      v-for="item in connectors.attention"
      :key="item.key"
      class="connector-notice__item"
    >
      <span>{{ item.name }}{{ connectorHealthLabel(item) }}</span>
      <RouterLink
        v-if="isAdmin"
        :to="{ path: '/capabilities', query: { tab: 'connectors', connector: item.key } }"
      >
        {{ connectorActionLabel(item) }}
      </RouterLink>
      <span v-else>请联系管理员处理</span>
    </div>
  </aside>
</template>

<style scoped lang="scss">
.connector-notice {
  margin: 12px 24px 0;
  padding: 12px 16px;
  border: 1px solid #ded6b6;
  border-radius: 6px;
  background: #faf6e8;
  color: #645731;
  font-size: 13px;
  &__item { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
  a { color: #244737; }
}
</style>
