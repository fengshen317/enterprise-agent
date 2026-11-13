<script setup lang="ts">
import type { Agent } from '../../api/agents'

defineProps<{ agent: Agent | null; open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const risks: Record<string, string> = { read: '只读', write: '写入 · 需审批', delete: '删除 · 需审批', external: '外发 · 需审批' }
</script>

<template>
  <aside
    v-if="open"
    class="capability-drawer"
    aria-label="本次能力"
  >
    <header class="capability-drawer__head">
      <div>
        <span class="capability-drawer__eyebrow">CAPABILITIES</span>
        <h2>{{ agent?.name ?? '序列助手' }}</h2>
        <p>{{ agent ? `v${agent.version} · ${agent.model}` : '内置 Agent · 只读工具与知识库' }}</p>
      </div>
      <button
        type="button"
        aria-label="关闭能力面板"
        @click="emit('close')"
      >
        ×
      </button>
    </header>
    <section class="capability-drawer__section">
      <h3>工具</h3>
      <p
        v-if="!agent?.tools.length"
        class="capability-drawer__empty"
      >
        当前 Agent 未绑定工具，仅直接回答。
      </p>
      <ul v-else>
        <li
          v-for="tool in agent.tools"
          :key="tool.key"
        >
          <span>{{ tool.display_name }}</span>
          <em :class="'capability-drawer__risk--' + tool.risk">{{ risks[tool.risk] ?? tool.risk }}</em>
        </li>
      </ul>
    </section>
    <section class="capability-drawer__section">
      <h3>技能</h3>
      <p
        v-if="!agent?.skills.length"
        class="capability-drawer__empty"
      >
        未绑定技能。
      </p>
      <ul v-else>
        <li
          v-for="skill in agent.skills"
          :key="skill.id"
        >
          <span>{{ skill.name }} · v{{ skill.version }}</span>
          <em>{{ skill.activation === 'always' ? '常驻' : '按需加载' }}</em>
        </li>
      </ul>
    </section>
    <section class="capability-drawer__section">
      <h3>知识库范围</h3>
      <p class="capability-drawer__empty">
        {{ agent?.knowledge_scope.mode === 'selected' ? `指定 ${agent.knowledge_scope.kb_ids.length} 个知识库` : '当前空间全部可读知识库' }}
      </p>
    </section>
    <section class="capability-drawer__section">
      <h3>安全策略</h3>
      <p class="capability-drawer__empty">
        只读工具自动执行；写入、删除和外发工具会先请求你的批准，并留下审批记录。
      </p>
    </section>
  </aside>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.capability-drawer {
  width: 300px; border-left: 1px solid $line; background: #f7f8f2; padding: 22px 20px; overflow-y: auto;
  &__head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; padding-bottom: 16px; border-bottom: 1px solid $line; button { border: 0; background: none; font-size: 20px; color: $muted; } h2 { font-size: 18px; margin: 7px 0 5px; font-weight: 500; } p { color: $muted; font-size: 11px; } }
  &__eyebrow { font-size: 9px; letter-spacing: 1.6px; color: #859576; }
  &__section { padding: 16px 0; border-bottom: 1px solid #e5eadf; h3 { font-size: 12px; color: $green; margin-bottom: 10px; } ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 9px; } li { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; line-height: 1.6; } em { color: $muted; font-style: normal; font-size: 10px; text-align: right; } }
  &__empty { color: $muted; font-size: 11px; line-height: 1.8; }
  &__risk--write, &__risk--delete, &__risk--external { color: #9a6a35; }
}
</style>
