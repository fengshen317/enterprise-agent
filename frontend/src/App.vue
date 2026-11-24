<script setup lang="ts">
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { computed, ref, watch } from 'vue'
import { useAuthStore } from './stores/auth'
import ConnectorNotice from './components/ConnectorNotice.vue'
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const error = ref('')
const mobileMenuOpen = ref(false)
const menuButton = ref<HTMLButtonElement | null>(null)

function dismissMenu(event: KeyboardEvent): void {
  if (!mobileMenuOpen.value) return
  event.preventDefault()
  mobileMenuOpen.value = false
  menuButton.value?.focus()
}

function closeMenuFromLink(event: MouseEvent): void {
  if (event.target instanceof Element && event.target.closest('a')) mobileMenuOpen.value = false
}

watch(() => route.path, () => { mobileMenuOpen.value = false })
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
    :class="{ 'workspace--knowledge': route.path === '/knowledge' || route.path.startsWith('/knowledge/') }"
  >
    <a
      class="workspace__skip"
      href="#workspace-content"
    >跳到主要内容</a>
    <aside
      class="workspace__sidebar"
      @keydown.esc="dismissMenu"
    >
      <div class="workspace__top">
        <div class="workspace__brand">
          <span class="workspace__mark">≋</span><div>序列<small>AGENT WORKSPACE</small></div>
        </div>
        <button
          ref="menuButton"
          class="workspace__menu-toggle"
          type="button"
          aria-controls="workspace-navigation"
          :aria-expanded="mobileMenuOpen"
          @click="mobileMenuOpen = !mobileMenuOpen"
        >
          {{ mobileMenuOpen ? '收起菜单' : '菜单' }}
        </button>
      </div>
      <div
        id="workspace-navigation"
        class="workspace__navigation"
        :class="{ 'workspace__navigation--open': mobileMenuOpen }"
      >
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
        <nav
          class="workspace__links"
          aria-label="工作空间导航"
          @click="closeMenuFromLink"
        >
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
            :class="{ 'workspace__nav--current': route.path.startsWith('/knowledge/') }"
            to="/knowledge"
          >▤ 知识库</RouterLink>
          <RouterLink
            class="workspace__nav"
            to="/materials"
          >▧ 我的材料</RouterLink>
          <RouterLink
            class="workspace__nav"
            to="/schedules"
          >⏱ 定时任务</RouterLink>
          <RouterLink
            class="workspace__nav"
            to="/channels/wechat"
          >✉ 微信</RouterLink>
          <RouterLink
            class="workspace__nav"
            to="/system"
          >◈ 系统概览</RouterLink>
          <RouterLink
            v-if="auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active'"
            class="workspace__nav"
            to="/admin/members"
          >⚙ 后台管理</RouterLink>
          <RouterLink
            v-if="auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active'"
            class="workspace__nav workspace__nav--admin-child"
            to="/admin/system-logs"
          >系统日志</RouterLink>
          <RouterLink
            v-if="auth.session?.system_admin === true"
            class="workspace__nav workspace__nav--admin-child"
            to="/admin/api-settings"
          >API 配置</RouterLink>
        </nav>
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
      </div>
    </aside>
    <main
      id="workspace-content"
      class="workspace__main"
      tabindex="-1"
    >
      <header class="workspace__header">
        <span>{{ route.meta.admin ? '后台管理' : '工作空间' }} / {{ sectionTitle }}</span><span class="workspace__stage">对话 · 规划 · 协作</span>
      </header>
      <ConnectorNotice />
      <RouterView :key="(auth.session?.user.id ?? '') + auth.selectedWorkspaceId" />
    </main>
  </div>
</template>

<style scoped lang="scss">
.workspace__space label { display: block; font-size: 10px; margin-bottom: 9px; color: #a9bba2; }
.workspace__space select { width: 100%; background: #244737; color: #e5eddb; border: 1px solid #58745a; border-radius: 4px; padding: 8px; font: inherit; }
.workspace__account { margin: auto 10px 20px; padding-top: 20px; font-size: 12px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.workspace__account button { border: 0; background: none; color: #c4d8b5; padding: 0; font-size: 11px; text-decoration: underline; }
.workspace__nav--admin-child {
  margin-left: 12px; padding: 9px 14px; border: 1px solid transparent; background: transparent;
  &::before { content: ''; display: inline-block; width: 6px; height: 6px; margin: 0 8px 2px 0; border-radius: 50%; background: #b5d5a5; }
  &:hover, &[aria-current='page'] { border-color: #557763; background: #2b5143; }
}
</style>
