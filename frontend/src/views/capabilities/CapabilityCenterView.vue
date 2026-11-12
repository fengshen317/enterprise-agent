<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { listSkills, type Skill } from '../../api/skills'
import { listTools, setToolEnabled, type Tool } from '../../api/tools'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const tab = computed(() => route.query.tab === 'skills' ? 'skills' : 'tools')
const tools = ref<Tool[]>([])
const skills = ref<Skill[]>([])
const loading = ref(false)
const error = ref('')
const busy = ref('')
const isAdmin = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
const riskLabels: Record<string, string> = { read: '只读', write: '写入', delete: '删除', external: '外发' }

function showError(value: unknown): void { error.value = value instanceof Error ? value.message : '操作失败，请重试' }
async function load(): Promise<void> {
  loading.value = true; error.value = ''
  try {
    const [toolList, skillList] = await Promise.all([listTools(), listSkills().catch(() => [] as Skill[])])
    tools.value = toolList; skills.value = skillList
  } catch (value) { showError(value) }
  finally { loading.value = false }
}
function switchTab(value: 'tools' | 'skills'): void {
  void router.replace({ query: { ...route.query, tab: value } })
}
async function toggle(tool: Tool): Promise<void> {
  if (busy.value || !isAdmin.value) return
  busy.value = tool.key
  try { await setToolEnabled(tool.key, !tool.enabled); await load() } catch (value) { showError(value) }
  finally { busy.value = '' }
}
watch(() => route.query.tab, () => { if (!loading.value) void load() })
onMounted(load)
</script>

<template>
  <section class="capabilities">
    <header class="capabilities__heading">
      <div>
        <span class="capabilities__eyebrow">CAPABILITY CENTER</span>
        <h1>能力中心</h1>
        <p>内置工具编目与版本化技能。工具由服务端实现，技能是 Markdown/SOP；外部连接将在后续版本加入。</p>
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
        type="button"
        :class="{ 'capabilities__tab--active': tab === 'tools' }"
        @click="switchTab('tools')"
      >
        工具目录
      </button>
      <button
        type="button"
        :class="{ 'capabilities__tab--active': tab === 'skills' }"
        @click="switchTab('skills')"
      >
        技能库
      </button>
    </nav>
    <p
      v-if="error"
      class="capabilities__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="loading"
      class="capabilities__hint"
    >
      正在读取能力…
    </p>

    <div
      v-else-if="tab === 'tools'"
      class="capabilities__table-wrap"
    >
      <table class="capabilities__table">
        <thead>
          <tr>
            <th>工具</th><th>风险</th><th>审批</th><th>超时</th><th>单次配额</th><th>被 Agent 使用</th><th v-if="isAdmin">
              空间启用
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="tool in tools"
            :key="tool.key"
          >
            <td><strong>{{ tool.display_name }}</strong><small>{{ tool.key }} · v{{ tool.version }}</small><p>{{ tool.description }}</p></td>
            <td>{{ riskLabels[tool.risk] ?? tool.risk }}</td>
            <td>{{ tool.approval === 'always' ? '逐次审批' : '自动执行' }}</td>
            <td>{{ tool.timeout_seconds }}s</td>
            <td>{{ tool.quota_per_run }}</td>
            <td>{{ tool.used_by_agents }}</td>
            <td v-if="isAdmin">
              <button
                type="button"
                :disabled="busy === tool.key"
                @click="toggle(tool)"
              >
                {{ tool.enabled ? '停用' : '启用' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
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
  &__tabs { display: flex; gap: 18px; margin: 20px 0 14px; button { border: 0; background: none; color: $muted; font-size: 13px; padding: 6px 2px; border-bottom: 2px solid transparent; } &--active { color: $green; border-bottom-color: $green; } }
  &__table-wrap { overflow-x: auto; border: 1px solid $line; border-radius: 8px; background: #fff; }
  &__table { width: 100%; border-collapse: collapse; font-size: 12px; th { text-align: left; padding: 12px 14px; background: $paper; color: $muted; font-weight: 500; white-space: nowrap; } td { padding: 14px; border-top: 1px solid #edf0e9; vertical-align: top; } strong { font-weight: 500; } small { display: block; color: $muted; margin-top: 4px; font-size: 10px; } p { color: $muted; font-size: 11px; line-height: 1.6; margin-top: 6px; max-width: 420px; } button { border: 0; background: none; color: $green; text-decoration: underline; font-size: 12px; } }
  &__hint { color: $muted; font-size: 12px; line-height: 1.9; margin-top: 16px; }
  &__error { color: #8b493c; background: #faf0e8; border: 1px solid #e8d7c7; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-top: 16px; }
  &__boundary { border-left: 2px solid #cfdcc4; padding: 0 0 0 12px; color: $muted; font-size: 11px; line-height: 1.9; margin: 16px; }
}
@media (max-width: 700px) { .capabilities__heading { flex-direction: column; align-items: flex-start; } }
</style>
