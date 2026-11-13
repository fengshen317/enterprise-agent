<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { archiveAgent, listAgents, type Agent } from '../../api/agents'

const router = useRouter()
const agents = ref<Agent[]>([])
const loading = ref(false)
const error = ref('')
const busy = ref('')

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}
async function load(): Promise<void> {
  loading.value = true; error.value = ''
  try { agents.value = await listAgents() } catch (value) { showError(value) }
  finally { loading.value = false }
}
async function archive(item: Agent): Promise<void> {
  if (busy.value || !confirm(`归档 Agent「${item.name}」？已有对话会回到内置助手。`)) return
  busy.value = item.id
  try { await archiveAgent(item.id); await load() } catch (value) { showError(value) }
  finally { busy.value = '' }
}
onMounted(load)
</script>

<template>
  <section class="agents">
    <header class="agents__heading">
      <div>
        <span class="agents__eyebrow">AGENT PROFILES</span>
        <h1>智能体</h1>
        <p>为不同任务配置大脑：职责指令、工具、技能与知识库范围。每个对话可以选择一个 Agent。</p>
      </div>
      <button
        type="button"
        class="agents__primary"
        @click="router.push('/agents/new')"
      >
        ＋ 新建 Agent
      </button>
    </header>
    <p
      v-if="error"
      class="agents__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="loading"
      class="agents__hint"
    >
      正在读取 Agent…
    </p>
    <div
      v-else-if="!agents.length"
      class="agents__empty"
    >
      <span>◎</span>
      <h2>还没有可用的 Agent。</h2>
      <p>先创建一个私有 Agent，绑定只读工具或技能，然后在对话中选择它。</p>
    </div>
    <div
      v-else
      class="agents__table-wrap"
    >
      <table class="agents__table">
        <thead>
          <tr><th>Agent</th><th>可见性</th><th>能力</th><th>模型</th><th>版本</th><th>更新时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="item in agents"
            :key="item.id"
          >
            <td>
              <strong>{{ item.icon }} {{ item.name }}</strong>
              <small>{{ item.description || '未填写描述' }}</small>
            </td>
            <td>{{ item.visibility === 'workspace' ? '空间共享' : '私有' }}</td>
            <td>{{ item.tools.length }} 个工具 · {{ item.skills.length }} 个技能</td>
            <td>{{ item.model || '默认模型' }}</td>
            <td>v{{ item.version }}</td>
            <td>{{ new Date(item.updated_at).toLocaleString() }}</td>
            <td class="agents__row-actions">
              <button
                type="button"
                @click="router.push('/agents/' + item.id)"
              >
                打开
              </button>
              <button
                v-if="item.can_manage"
                type="button"
                :disabled="busy === item.id"
                @click="archive(item)"
              >
                归档
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="agents__boundary">
      空间 Agent 由管理员创建和管理；私有 Agent 仅创建者可见。归档不会删除历史运行快照。
    </p>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.agents {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; padding-bottom: 24px; border-bottom: 1px solid $line; h1 { font-size: clamp(28px, 3vw, 40px); font-weight: 400; margin: 10px 0; } p { color: $muted; font-size: 12px; line-height: 1.9; max-width: 680px; } }
  &__eyebrow { font-size: 9px; letter-spacing: 2px; color: #859576; }
  &__primary { background: #31533d; color: #edf4e4; border: 0; border-radius: 5px; padding: 11px 17px; font-size: 12px; white-space: nowrap; }
  &__hint { color: $muted; font-size: 12px; margin-top: 22px; }
  &__empty { border: 1px dashed #cbd7be; border-radius: 8px; padding: 48px 24px; text-align: center; margin-top: 24px; span { font-size: 36px; color: #8aa071; } h2 { font-size: 20px; font-weight: 400; margin: 14px 0 8px; } p { color: $muted; font-size: 12px; } }
  &__table-wrap { overflow-x: auto; margin-top: 24px; border: 1px solid $line; border-radius: 8px; background: #fff; }
  &__table { width: 100%; border-collapse: collapse; font-size: 12px; th { text-align: left; padding: 12px 14px; background: $paper; color: $muted; font-weight: 500; white-space: nowrap; } td { padding: 14px; border-top: 1px solid #edf0e9; vertical-align: top; } strong { font-weight: 500; } small { display: block; color: $muted; margin-top: 5px; font-size: 11px; line-height: 1.6; } }
  &__row-actions { white-space: nowrap; button { border: 0; background: none; color: $green; font-size: 12px; padding: 2px 7px; text-decoration: underline; } }
  &__error { color: #8b493c; background: #faf0e8; border: 1px solid #e8d7c7; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-top: 20px; }
  &__boundary { border-left: 2px solid #cfdcc4; padding-left: 12px; color: $muted; font-size: 11px; line-height: 1.9; margin-top: 20px; }
}
@media (max-width: 700px) { .agents__heading { align-items: flex-start; flex-direction: column; } }
</style>
