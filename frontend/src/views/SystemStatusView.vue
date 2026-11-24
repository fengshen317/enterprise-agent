<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError } from '../api/client'
import { getHealth } from '../api/health'

interface CheckState { state: 'pending' | 'success' | 'error'; detail: string; requestId?: string | null }
const checking = ref(false)
const lastChecked = ref('尚未检查')
const api = ref<CheckState>({ state: 'pending', detail: '等待检测 API 进程' })
const database = ref<CheckState>({ state: 'pending', detail: '等待检测数据库与迁移' })

async function checkSystem(): Promise<void> {
  if (checking.value) return
  checking.value = true
  const results = await Promise.allSettled([getHealth('live'), getHealth('ready')])
  results.forEach((result, index) => {
    const target = index === 0 ? api : database
    if (result.status === 'fulfilled') {
      target.value = { state: 'success', detail: index === 0 ? result.value.service + ' · v' + result.value.version : '连接成功，基础迁移已完成' }
    } else {
      const error: unknown = result.reason
      target.value = { state: 'error', detail: error instanceof Error ? error.message : '检查失败', requestId: error instanceof ApiError ? error.requestId : null }
    }
  })
  lastChecked.value = new Date().toLocaleTimeString('zh-CN')
  checking.value = false
}

onMounted(checkSystem)
</script>

<template>
  <div class="overview">
    <header class="overview__heading">
      <div>
        <h1>系统概览</h1>
        <p class="overview__description">
          检查 API 服务、数据库连接和迁移状态。
        </p>
      </div>
      <button
        class="overview__refresh"
        :disabled="checking"
        @click="checkSystem"
      >
        {{ checking ? '检查中…' : '↻ 重新检查' }}
      </button>
    </header>
    <div class="overview__section-heading">
      <h2>运行状态</h2><span>最近检查：{{ lastChecked }}</span>
    </div>
    <div
      class="overview__checks"
      aria-live="polite"
    >
      <article
        v-for="(item, index) in [api, database]"
        :key="index"
        class="status-card"
      >
        <div class="status-card__top">
          <span>{{ index === 0 ? 'API 服务' : '数据库与迁移' }}</span><span :class="['status-card__badge', 'status-card__badge--' + item.state]">{{ item.state === 'success' ? '正常' : item.state === 'error' ? '未就绪' : '待检查' }}</span>
        </div>
        <h3>{{ index === 0 ? 'FastAPI' : 'PostgreSQL' }}</h3><p>{{ item.detail }}</p>
        <code v-if="item.requestId">请求标识：{{ item.requestId }}</code>
      </article>
    </div>
  </div>
</template>
