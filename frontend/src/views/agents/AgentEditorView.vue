<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { ApiError } from '../../api/client'
import { createAgent, getAgent, updateAgent, type Agent, type AgentInput, type SkillActivation } from '../../api/agents'
import { listKnowledgeBases, type KnowledgeBase } from '../../api/knowledge'
import { listSkills, type Skill } from '../../api/skills'
import { listTools, type Tool } from '../../api/tools'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const agentId = computed(() => typeof route.params.agentId === 'string' && route.params.agentId !== 'new' ? route.params.agentId : '')
const agent = ref<Agent | null>(null)
const tools = ref<Tool[]>([])
const skills = ref<Skill[]>([])
const knowledgeBases = ref<KnowledgeBase[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const starterText = ref('')
const form = reactive<AgentInput>({
  name: '', description: '', icon: '◎', instructions: '', visibility: 'private', status: 'active', model: '',
  allow_planning: true, allow_delegation: true, tool_keys: [], skills: [], knowledge_mode: 'all_readable',
  knowledge_base_ids: [], starter_prompts: [],
})
const canEdit = computed(() => !agent.value || agent.value.can_manage)
const canWorkspace = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
const riskLabels: Record<string, string> = { read: '只读', write: '写入 · 需审批', delete: '删除 · 需审批', external: '外发 · 需审批' }

function showError(value: unknown): void {
  error.value = value instanceof ApiError && value.status === 409
    ? 'Agent 已被其他人修改，请刷新后重试。'
    : value instanceof Error ? value.message : '操作失败，请重试'
}
function populate(value: Agent): void {
  agent.value = value
  form.name = value.name
  form.description = value.description
  form.icon = value.icon
  form.instructions = value.instructions ?? ''
  form.visibility = value.visibility
  form.status = value.status
  form.model = value.model
  form.allow_planning = value.allow_planning
  form.allow_delegation = value.allow_delegation
  form.tool_keys = value.tools.map(item => item.key)
  form.skills = value.skills.map(item => ({ id: item.id, activation: item.activation }))
  form.knowledge_mode = value.knowledge_scope.mode
  form.knowledge_base_ids = [...value.knowledge_scope.kb_ids]
  form.starter_prompts = [...value.starter_prompts]
  starterText.value = value.starter_prompts.join('\n')
}
async function load(): Promise<void> {
  loading.value = true; error.value = ''
  try {
    const [toolList, skillList, kbList] = await Promise.all([
      listTools(),
      listSkills().catch(() => [] as Skill[]),
      listKnowledgeBases().catch(() => [] as KnowledgeBase[]),
    ])
    tools.value = toolList
    skills.value = skillList.filter(item => item.status !== 'archived')
    knowledgeBases.value = kbList
    if (agentId.value) populate(await getAgent(agentId.value))
  } catch (value) { showError(value) }
  finally { loading.value = false }
}
function toggleTool(key: string): void {
  const index = form.tool_keys.indexOf(key)
  if (index >= 0) form.tool_keys.splice(index, 1)
  else form.tool_keys.push(key)
}
function toggleSkill(id: string): void {
  const index = form.skills.findIndex(item => item.id === id)
  if (index >= 0) form.skills.splice(index, 1)
  else form.skills.push({ id, activation: 'always' })
}
function isBound(id: string): boolean {
  return form.skills.some(item => item.id === id)
}
function skillActivation(id: string): SkillActivation {
  return form.skills.find(item => item.id === id)?.activation ?? 'always'
}
function setSkillActivation(id: string, value: string): void {
  const item = form.skills.find(row => row.id === id)
  if (item) item.activation = value as SkillActivation
}
function toggleKnowledge(id: string): void {
  const index = form.knowledge_base_ids.indexOf(id)
  if (index >= 0) form.knowledge_base_ids.splice(index, 1)
  else form.knowledge_base_ids.push(id)
}
async function save(): Promise<void> {
  if (saving.value || !canEdit.value) return
  if (!form.name.trim()) { error.value = '请填写 Agent 名称。'; return }
  saving.value = true; error.value = ''; notice.value = ''
  const input: AgentInput = {
    ...form,
    name: form.name.trim(),
    starter_prompts: starterText.value.split('\n').map(item => item.trim()).filter(Boolean).slice(0, 4),
  }
  try {
    const saved = agentId.value
      ? await updateAgent(agentId.value, { ...input, expected_version: agent.value?.version ?? 1 })
      : await createAgent(input)
    populate(saved)
    notice.value = '已保存。'
    if (!agentId.value) await router.replace('/agents/' + saved.id)
  } catch (value) { showError(value) }
  finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <section class="agent-editor">
    <header class="agent-editor__heading">
      <div>
        <button
          type="button"
          class="agent-editor__back"
          @click="router.push('/agents')"
        >
          ← 返回智能体
        </button>
        <span class="agent-editor__eyebrow">AGENT PROFILE</span>
        <h1>{{ agentId ? form.name || 'Agent 配置' : '新建 Agent' }}</h1>
        <p>配置大脑的职责、工具、技能和知识范围。保存后即可在新建对话时选择。</p>
      </div>
      <button
        type="button"
        class="agent-editor__primary"
        :disabled="saving || !canEdit"
        @click="save"
      >
        {{ saving ? '正在保存…' : '保存配置' }}
      </button>
    </header>
    <p
      v-if="error"
      class="agent-editor__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="agent-editor__notice"
    >
      {{ notice }}
    </p>
    <p
      v-if="loading"
      class="agent-editor__hint"
    >
      正在读取配置…
    </p>
    <p
      v-else-if="!canEdit"
      class="agent-editor__hint"
    >
      你可以查看该 Agent 的能力，但只有创建者或空间管理员可以修改。
    </p>

    <div
      v-if="!loading"
      class="agent-editor__grid"
    >
      <section class="agent-editor__card">
        <h2>基本信息</h2>
        <div class="agent-editor__fields">
          <label>名称<input
            v-model="form.name"
            :disabled="!canEdit"
            maxlength="100"
          /></label>
          <label>图标<input
            v-model="form.icon"
            :disabled="!canEdit"
            maxlength="32"
          /></label>
          <label class="agent-editor__wide">描述<textarea
            v-model="form.description"
            :disabled="!canEdit"
            rows="2"
            maxlength="2000"
          /></label>
          <label>可见性
            <select
              v-model="form.visibility"
              :disabled="!canEdit"
            >
              <option value="private">私有（仅自己）</option>
              <option
                v-if="canWorkspace"
                value="workspace"
              >空间共享（管理员可建）</option>
            </select>
          </label>
          <label>模型<input
            :value="form.model || '默认模型'"
            disabled
          /></label>
        </div>
      </section>

      <section class="agent-editor__card">
        <h2>职责与指令</h2>
        <p class="agent-editor__hint">
          说明 Agent 的角色、工作方式和输出要求。不要在这里粘贴密钥。
        </p>
        <textarea
          v-model="form.instructions"
          class="agent-editor__instructions"
          :disabled="!canEdit"
          rows="7"
          maxlength="8000"
          placeholder="例如：你是制度问答助手。先检索知识库，再给出带来源的结论。"
        />
        <div class="agent-editor__behaviour">
          <label><input
            v-model="form.allow_planning"
            type="checkbox"
            :disabled="!canEdit"
          /> 允许规划复杂任务</label>
          <label><input
            v-model="form.allow_delegation"
            type="checkbox"
            :disabled="!canEdit"
          /> 允许委派子 Agent</label>
        </div>
      </section>

      <section class="agent-editor__card agent-editor__card--wide">
        <h2>工具</h2>
        <p class="agent-editor__hint">
          只读工具自动执行；写入、删除或外发工具会在运行时请求用户审批。不可用或已停用的工具不能绑定。
        </p>
        <div class="agent-editor__options">
          <label
            v-for="tool in tools"
            :key="tool.key"
            class="agent-editor__option"
            :class="{ 'agent-editor__option--disabled': !tool.enabled }"
          >
            <input
              type="checkbox"
              :checked="form.tool_keys.includes(tool.key)"
              :disabled="!canEdit || !tool.enabled"
              @change="toggleTool(tool.key)"
            />
            <span>
              <strong>{{ tool.display_name }}</strong>
              <small>{{ tool.description }}</small>
            </span>
            <em>{{ riskLabels[tool.risk] ?? tool.risk }}</em>
          </label>
        </div>
      </section>

      <section class="agent-editor__card">
        <h2>技能</h2>
        <p class="agent-editor__hint">
          技能是版本化的 Markdown/SOP。常驻技能进入系统提示词，按需技能由模型用 load_skill 加载。
        </p>
        <div
          v-if="!skills.length"
          class="agent-editor__hint"
        >
          还没有已发布技能，请先在能力中心创建。
        </div>
        <div
          v-for="skill in skills"
          :key="skill.id"
          class="agent-editor__skill"
        >
          <label>
            <input
              type="checkbox"
              :checked="form.skills.some(item => item.id === skill.id)"
              :disabled="!canEdit || (skill.status !== 'published' && !isBound(skill.id))"
              @change="toggleSkill(skill.id)"
            />
            <span>{{ skill.name }} · v{{ skill.current_version }} <small>{{ skill.status === 'published' ? '已发布' : '草稿' }}</small></span>
          </label>
          <select
            v-if="form.skills.some(item => item.id === skill.id)"
            :value="skillActivation(skill.id)"
            :disabled="!canEdit"
            @change="setSkillActivation(skill.id, ($event.target as HTMLSelectElement).value)"
          >
            <option value="always">
              常驻
            </option>
            <option value="on_demand">
              按需
            </option>
          </select>
        </div>
      </section>

      <section class="agent-editor__card">
        <h2>知识库范围</h2>
        <label class="agent-editor__radio">
          <input
            v-model="form.knowledge_mode"
            type="radio"
            value="all_readable"
            :disabled="!canEdit"
          /> 当前空间全部可读知识库
        </label>
        <label class="agent-editor__radio">
          <input
            v-model="form.knowledge_mode"
            type="radio"
            value="selected"
            :disabled="!canEdit"
          /> 指定知识库
        </label>
        <div
          v-if="form.knowledge_mode === 'selected'"
          class="agent-editor__kb-list"
        >
          <label
            v-for="kb in knowledgeBases"
            :key="kb.id"
          >
            <input
              type="checkbox"
              :checked="form.knowledge_base_ids.includes(kb.id)"
              :disabled="!canEdit"
              @change="toggleKnowledge(kb.id)"
            />
            {{ kb.name }} <small>{{ kb.visibility === 'workspace' ? '空间' : '私有' }}</small>
          </label>
          <p
            v-if="!knowledgeBases.length"
            class="agent-editor__hint"
          >
            暂无可用知识库。
          </p>
        </div>
      </section>

      <section class="agent-editor__card agent-editor__card--wide">
        <h2>欢迎页示例</h2>
        <p class="agent-editor__hint">
          每行一条，最多 4 条；显示在新对话欢迎页。
        </p>
        <textarea
          v-model="starterText"
          class="agent-editor__instructions"
          :disabled="!canEdit"
          rows="3"
          placeholder="查询最近七天的运行统计"
        />
      </section>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.agent-editor {
  max-width: 1180px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; padding-bottom: 22px; border-bottom: 1px solid $line; h1 { font-size: clamp(26px, 3vw, 38px); font-weight: 400; margin: 8px 0; } p { color: $muted; font-size: 12px; line-height: 1.9; max-width: 680px; } }
  &__back { border: 0; background: none; color: $green; font-size: 12px; padding: 0 0 12px; }
  &__eyebrow { font-size: 9px; letter-spacing: 2px; color: #859576; }
  &__primary { background: #31533d; color: #edf4e4; border: 0; border-radius: 5px; padding: 11px 18px; font-size: 12px; white-space: nowrap; }
  &__grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 24px; }
  &__card { border: 1px solid $line; background: #fff; border-radius: 8px; padding: 20px; h2 { font-size: 14px; color: $green; margin-bottom: 14px; font-weight: 500; } &--wide { grid-column: 1 / -1; } }
  &__fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; label { font-size: 11px; color: $green; display: flex; flex-direction: column; gap: 6px; } input, select, textarea { border: 1px solid #d6dfd0; background: $paper; border-radius: 4px; padding: 8px; font: inherit; color: $ink; } }
  &__wide { grid-column: 1 / -1; }
  &__instructions { width: 100%; border: 1px solid #d6dfd0; background: $paper; border-radius: 5px; padding: 11px; font: inherit; font-size: 12px; line-height: 1.8; color: $ink; resize: vertical; }
  &__behaviour { display: flex; gap: 20px; margin-top: 12px; font-size: 12px; color: #516b42; }
  &__options { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 9px; }
  &__option { display: grid; grid-template-columns: 18px minmax(0, 1fr) auto; align-items: start; gap: 9px; border: 1px solid #e0e6da; border-radius: 6px; padding: 10px; font-size: 12px; span { display: flex; flex-direction: column; gap: 4px; } small { color: $muted; font-size: 10px; line-height: 1.6; } em { color: #9a6a35; font-size: 10px; font-style: normal; text-align: right; } &--disabled { opacity: .5; } }
  &__skill { display: flex; justify-content: space-between; align-items: center; gap: 10px; border-bottom: 1px solid #edf0e9; padding: 9px 0; font-size: 12px; label { display: flex; gap: 8px; align-items: center; } small { color: $muted; font-size: 10px; } select { border: 1px solid #d6dfd0; background: $paper; border-radius: 4px; padding: 5px; font-size: 11px; } }
  &__radio { display: block; font-size: 12px; margin: 8px 0; }
  &__kb-list { display: grid; gap: 7px; margin-top: 10px; font-size: 12px; label { display: flex; gap: 7px; align-items: center; } small { color: $muted; font-size: 10px; } }
  &__hint { color: $muted; font-size: 11px; line-height: 1.8; margin-bottom: 10px; }
  &__error { color: #8b493c; background: #faf0e8; border: 1px solid #e8d7c7; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-top: 18px; }
  &__notice { color: #386b4f; background: #edf4e6; border: 1px solid #d6e5c9; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-top: 18px; }
}
@media (max-width: 800px) { .agent-editor__grid { grid-template-columns: minmax(0, 1fr); } .agent-editor__heading { flex-direction: column; align-items: flex-start; } }
</style>
