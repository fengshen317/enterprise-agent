<script setup lang="ts">
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { computed, ref } from 'vue'
import { useAuthStore } from './stores/auth'
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const error = ref('')
const sectionTitle = computed(() => typeof route.meta.title === 'string' ? route.meta.title : '问答')
async function signOut(): Promise<void> {
  try { await auth.signOut(); await router.replace('/login') } catch (value) { error.value = value instanceof Error ? value.message : '退出失败，请重试' }
}
async function changeWorkspace(event: Event): Promise<void> {
  auth.selectWorkspace((event.target as HTMLSelectElement).value)
  await router.replace('/chat')
}
</script>

<template>
  <RouterView v-if="route.name === 'login'" />
  <div
    v-else
    class="workspace"
  >
    <aside class="workspace__sidebar">
      <div class="workspace__brand">
        <span class="workspace__mark">≋</span><div>序列<small>AGENT WORKSPACE</small></div>
      </div>
      <div class="workspace__space">
        <label for="workspace-select">工作空间</label>
        <select
          id="workspace-select"
          :value="auth.selectedWorkspaceId"
          aria-label="切换工作空间"
          @change="changeWorkspace"
        >
          <option
            v-for="item in auth.session?.workspaces"
            :key="item.id"
            :value="item.id"
          >
            {{ item.name }}{{ item.status === 'archived' ? '（归档）' : '' }}
          </option>
        </select>
        <p v-if="!auth.session?.workspaces.length">
          暂无可用空间，请联系管理员。
        </p>
      </div>
      <p class="workspace__label">
        工作空间
      </p>
      <RouterLink
        class="workspace__nav"
        to="/chat"
      >↗ 问答工作台</RouterLink>
      <RouterLink
        class="workspace__nav"
        to="/agents"
      >◎ Agent 管理</RouterLink>
      <RouterLink
        class="workspace__nav"
        to="/capabilities"
      >✦ 能力中心</RouterLink>
      <RouterLink
        class="workspace__nav"
        to="/knowledge"
      >▤ 知识库</RouterLink>
      <RouterLink
        class="workspace__nav"
        to="/schedules"
      >⏱ 定时任务</RouterLink>
      <RouterLink
        class="workspace__nav"
        to="/system"
      >◈ 系统概览</RouterLink>
      <RouterLink
        v-if="auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active'"
        class="workspace__nav"
        to="/admin/members"
      >⚙ 后台管理</RouterLink>
      <div class="workspace__next">
        对话 · 智能体 · 能力扩展
      </div>
      <div class="workspace__account">
        <span>{{ auth.session?.user.display_name }}</span><button
          type="button"
          @click="signOut"
        >
          退出登录
        </button><p
          v-if="error"
          role="alert"
        >
          {{ error }}
        </p>
      </div>
      <div class="workspace__sidebar-footer">
        DEEP AGENTS<br />从一个问题，到完整交付。
      </div>
    </aside>
    <main class="workspace__main">
      <header class="workspace__header">
        <span>工作空间 / {{ sectionTitle }}</span><span class="workspace__stage">对话 · 规划 · 协作</span>
      </header>
      <RouterView :key="(auth.session?.user.id ?? '') + auth.selectedWorkspaceId" />
    </main>
  </div>
</template>

<style scoped lang="scss">
.workspace__space label { display: block; font-size: 10px; margin-bottom: 9px; color: #a9bba2; }
.workspace__space select { width: 100%; background: #244737; color: #e5eddb; border: 1px solid #58745a; border-radius: 4px; padding: 8px; font: inherit; }
.workspace__account { margin: auto 10px 20px; padding-top: 20px; font-size: 12px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.workspace__account button { border: 0; background: none; color: #c4d8b5; padding: 0; font-size: 11px; text-decoration: underline; }
</style>
