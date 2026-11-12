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
    <div class="overview__heading">
      <div>
        <p class="overview__eyebrow">
          BUILD WITH PURPOSE / 01
        </p><h1>让可靠的基础，承接每一个目标。</h1><p class="overview__description">
          企业 Agent 工作台 · 本地问答与工程状态
        </p>
      </div>
      <button
        class="overview__refresh"
        :disabled="checking"
        @click="checkSystem"
      >
        {{ checking ? '检查中…' : '↻ 重新检查' }}
      </button>
    </div>
    <section class="overview__intro">
      <div><span class="overview__pill">工程基础</span><h2>先把系统运行起来。<br />再让智能进入工作。</h2><p>连接应用、数据库与开发流程，为真实问答、工具使用和持续执行建立清晰边界。</p></div>
      <div class="overview__number">
        01<span> / 07 阶段</span>
      </div>
    </section>
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
    <div class="overview__section-heading">
      <h2>从基础到交付</h2><span>按阶段推进，每一步都可验证</span>
    </div>
    <div class="overview__roadmap">
      <article><span>01 / 已完成</span><h3>工程初始化</h3><p>应用骨架、配置、日志、数据库迁移与自动检查。</p></article>
      <article><span>02 / 当前阶段</span><h3>真实问答</h3><p>真实模型、连续追问、流式回复与历史记录。</p></article>
      <article><span>03—07 / 后续阶段</span><h3>从协作到可靠执行</h3><p>身份权限、工具知识、后台任务、审批与交付。</p></article>
    </div>
    <footer class="overview__footer">
      问答工作台已接入模型与历史记录；任务与审批在后续阶段实现。问答接口已启用登录和资源归属校验。
    </footer>
  </div>
</template>
