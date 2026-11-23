<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { listSkills, type Skill } from '../../api/skills'
import { listTools, setToolEnabled, type Tool } from '../../api/tools'
import ConnectorsView from '../connectors/ConnectorsView.vue'

type CapabilityTab = 'tools' | 'skills' | 'connectors'
const sections = {
  tools: { label: '工具', description: '查看 Agent 可调用的工具、审批要求和空间启用状态。' },
  skills: { label: '技能', description: '维护版本化的 Markdown / SOP，让 Agent 按工作流程执行。' },
  connectors: { label: '连接器', description: '接入外部服务，管理授权、配置和连接状态。' },
}
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const tab = computed<CapabilityTab>(() => {
  const value = route.query.tab
  return value === 'skills' || value === 'connectors' ? value : 'tools'
})
const source = computed(() => typeof route.query.source === 'string' ? route.query.source : '')
const tools = ref<Tool[]>([])
const skills = ref<Skill[]>([])
const loading = ref(false)
const error = ref('')
const busy = ref('')
const visibleTools = computed(() => source.value ? tools.value.filter(tool => tool.connector_key === source.value) : tools.value)
const isAdmin = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
const riskLabels: Record<string, string> = { read: '只读', write: '写入', delete: '删除', external: '外发' }

function showError(value: unknown): void { error.value = value instanceof Error ? value.message : '操作失败，请重试' }
let loadVersion = 0
async function load(section: CapabilityTab = tab.value): Promise<void> {
  const version = ++loadVersion
  loading.value = section !== 'connectors'
  error.value = ''
  if (section === 'connectors') return
  try {
    if (section === 'tools') {
      const items = await listTools()
      if (version === loadVersion) tools.value = items
    } else {
      const items = await listSkills()
      if (version === loadVersion) skills.value = items
    }
  } catch (value) { if (version === loadVersion) showError(value) }
  finally { if (version === loadVersion) loading.value = false }
}
function switchTab(value: CapabilityTab): void {
  if (value !== tab.value || source.value || route.query.connector) void router.push({ query: { tab: value } })
}
async function toggle(tool: Tool): Promise<void> {
  if (busy.value || !isAdmin.value) return
  busy.value = tool.key
  try {
    const updated = await setToolEnabled(tool.key, !tool.enabled)
    tools.value = tools.value.map(item => item.key === updated.key ? updated : item)
  } catch (value) { if (tab.value === 'tools') showError(value) }
  finally { busy.value = '' }
}
watch(tab, value => { void load(value) }, { immediate: true })
</script>

<template>
  <section class="capabilities">
    <header class="capabilities__heading">
      <div>
        <span class="capabilities__eyebrow">CAPABILITY CENTER</span>
        <h1>能力中心</h1>
        <p>管理工作空间的工具、技能和外部连接。为单个 Agent 分配能力，请前往「Agent 管理」。</p>
      </div>
      <button
        v-if="tab === 'skills'"
        type="button"
        class="capabilities__primary"
        @click="router.push('/capabilities/skills/new')"
      >
        ＋ 新建技能
      </button>
    </header>
    <nav
      class="capabilities__tabs"
      aria-label="能力分类"
    >
      <button
        v-for="(section, key) in sections"
        :key="key"
        type="button"
        :aria-current="tab === key ? 'page' : undefined"
        @click="switchTab(key)"
      >
        {{ section.label }}
      </button>
    </nav>
    <p class="capabilities__section-description">
      {{ sections[tab].description }}
    </p>
    <p
      v-if="error"
      class="capabilities__error"
      role="alert"
    >
      {{ error }}
    </p>
    <ConnectorsView
      v-if="tab === 'connectors'"
      embedded
    />
    <p
      v-else-if="loading"
      class="capabilities__hint"
    >
      正在读取能力…
    </p>

    <div
      v-else-if="tab === 'tools'"
      class="capabilities__table-wrap"
    >
      <div
        v-if="source"
        class="capabilities__filter"
      >
        <span>仅显示连接器工具：<code>{{ source }}</code></span>
        <RouterLink :to="{ path: '/capabilities', query: { tab: 'tools' } }">
          查看全部工具
        </RouterLink>
      </div>
      <table class="capabilities__table">
        <thead>
          <tr>
            <th>工具</th><th>来源</th><th>风险</th><th>审批</th><th>超时</th><th>单次配额</th><th>被 Agent 使用</th><th>
              空间状态
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="tool in visibleTools"
            :key="tool.key"
          >
            <td><strong>{{ tool.display_name }}</strong><small>{{ tool.key }} · v{{ tool.version }}</small><p>{{ tool.description }}</p></td>
            <td>
              <RouterLink
                v-if="tool.connector_key"
                :to="{ path: '/capabilities', query: { tab: 'connectors', connector: tool.connector_key } }"
                :title="tool.connector_key"
              >
                连接器
              </RouterLink>
              <span v-else>内置</span>
            </td>
            <td>{{ riskLabels[tool.risk] ?? tool.risk }}</td>
            <td>{{ tool.approval === 'always' ? '逐次审批' : '自动执行' }}</td>
            <td>{{ tool.timeout_seconds }}s</td>
            <td>{{ tool.quota_per_run }}</td>
            <td>{{ tool.used_by_agents }}</td>
            <td>
              <span>{{ tool.enabled ? '已启用' : '已停用' }}</span>
              <button
                v-if="isAdmin"
                type="button"
                :disabled="Boolean(busy)"
                @click="toggle(tool)"
              >
                {{ tool.enabled ? '停用' : '启用' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p
        v-if="!visibleTools.length && !error"
        class="capabilities__boundary"
      >
        {{ source ? '此连接器暂无当前账号可见的工具。请检查连接器的启用与授权状态。' : '暂无可见工具。' }}
      </p>
      <p class="capabilities__boundary">
        停用工具不会删除历史审计；新的运行装配会跳过被停用的工具。
      </p>
    </div>

    <div
      v-else
      class="capabilities__table-wrap"
    >
      <table class="capabilities__table">
        <thead>
          <tr><th>技能</th><th>可见性</th><th>状态</th><th>版本</th><th>默认激活</th><th>绑定范围</th><th>更新时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="skill in skills"
            :key="skill.id"
          >
            <td><strong>{{ skill.name }}</strong><small>{{ skill.key }}</small><p>{{ skill.description || '未填写描述' }}</p></td>
            <td>{{ skill.visibility === 'workspace' ? '空间共享' : '私有' }}</td>
            <td>{{ skill.status === 'published' ? '已发布' : skill.status === 'draft' ? '草稿' : '已归档' }}</td>
            <td>v{{ skill.current_version }}</td>
            <td>{{ skill.activation === 'always' ? '常驻' : '按需' }}</td>
            <td>{{ skill.tool_keys.length }} 工具 · {{ skill.knowledge_scope.mode === 'selected' ? skill.knowledge_scope.kb_ids.length + ' 知识库' : '全部知识库' }}</td>
            <td>{{ new Date(skill.updated_at).toLocaleString() }}</td>
            <td>
              <button
                type="button"
                @click="router.push('/capabilities/skills/' + skill.id)"
              >
                编辑
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p
        v-if="!skills.length"
        class="capabilities__hint"
      >
        还没有技能。先创建一份 Markdown/SOP，再发布并绑定到 Agent。
      </p>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.capabilities {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; padding-bottom: 24px; border-bottom: 1px solid $line; h1 { font-size: clamp(28px, 3vw, 40px); font-weight: 400; margin: 10px 0; } p { color: $muted; font-size: 12px; line-height: 1.9; max-width: 720px; } }
  &__eyebrow { font-size: 9px; letter-spacing: 2px; color: #859576; }
  &__primary { background: #31533d; color: #edf4e4; border: 0; border-radius: 5px; padding: 11px 17px; font-size: 12px; white-space: nowrap; }
  &__tabs { display: flex; gap: 6px; margin: 20px 0 10px; border-bottom: 1px solid $line; button { border: 0; background: none; color: $muted; font-size: 13px; padding: 10px 16px; border-bottom: 2px solid transparent; } button[aria-current='page'] { color: $green; border-bottom-color: $green; font-weight: 600; } }
  &__section-description { margin: 0 0 16px; color: $muted; font-size: 13px; line-height: 1.7; }
  &__filter { display: flex; flex-wrap: wrap; gap: 8px 16px; padding: 12px 14px; background: $paper; color: $muted; font-size: 12px; overflow-wrap: anywhere; }
  &__filter a, &__table a { color: $green; text-decoration: underline; }
  &__table td:last-child button { margin-inline-start: 8px; }
  &__table-wrap { overflow-x: auto; border: 1px solid $line; border-radius: 8px; background: #fff; }
  &__table { width: 100%; border-collapse: collapse; font-size: 12px; th { text-align: left; padding: 12px 14px; background: $paper; color: $muted; font-weight: 500; white-space: nowrap; } td { padding: 14px; border-top: 1px solid #edf0e9; vertical-align: top; } strong { font-weight: 500; } small { display: block; color: $muted; margin-top: 4px; font-size: 10px; } p { color: $muted; font-size: 11px; line-height: 1.6; margin-top: 6px; max-width: 420px; } button { border: 0; background: none; color: $green; text-decoration: underline; font-size: 12px; } }
  &__hint { color: $muted; font-size: 12px; line-height: 1.9; margin-top: 16px; }
  &__error { color: #8b493c; background: #faf0e8; border: 1px solid #e8d7c7; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-top: 16px; }
  &__boundary { border-left: 2px solid #cfdcc4; padding: 0 0 0 12px; color: $muted; font-size: 11px; line-height: 1.9; margin: 16px; }
}
@media (max-width: 700px) { .capabilities__heading { flex-direction: column; align-items: flex-start; } }
</style>
