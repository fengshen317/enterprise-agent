<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MarkdownBody from '../../components/MarkdownBody.vue'
import { listKnowledgeBases, type KnowledgeBase } from '../../api/knowledge'
import { createSkill, getSkill, listSkillVersions, publishSkill, updateSkill, type Skill, type SkillInput, type SkillVersion } from '../../api/skills'
import { listTools, type Tool } from '../../api/tools'

const route = useRoute()
const router = useRouter()
const skillId = computed(() => typeof route.params.skillId === 'string' && route.params.skillId !== 'new' ? route.params.skillId : '')
const skill = ref<Skill | null>(null)
const tools = ref<Tool[]>([])
const knowledgeBases = ref<KnowledgeBase[]>([])
const versions = ref<SkillVersion[]>([])
const loading = ref(false)
const saving = ref(false)
const publishing = ref(false)
const error = ref('')
const notice = ref('')
const form = reactive<SkillInput>({
  key: '', name: '', description: '', visibility: 'private', content_md: '', tool_keys: [],
  knowledge_mode: 'all_readable', knowledge_base_ids: [], activation: 'always', token_budget: 4000, change_note: '',
})
const canEdit = computed(() => !skill.value || skill.value.can_manage)
const riskLabels: Record<string, string> = { read: '只读', write: '写入', delete: '删除', external: '外发' }

function showError(value: unknown): void { error.value = value instanceof Error ? value.message : '操作失败，请重试' }
function populate(value: Skill): void {
  skill.value = value
  form.key = value.key
  form.name = value.name
  form.description = value.description
  form.visibility = value.visibility
  form.content_md = value.content_md ?? ''
  form.tool_keys = [...value.tool_keys]
  form.knowledge_mode = value.knowledge_scope.mode
  form.knowledge_base_ids = [...value.knowledge_scope.kb_ids]
  form.activation = value.activation
  form.token_budget = value.token_budget
}
function toggleTool(key: string): void {
  const index = form.tool_keys.indexOf(key)
  if (index >= 0) form.tool_keys.splice(index, 1)
  else form.tool_keys.push(key)
}
function toggleKnowledge(id: string): void {
  const index = form.knowledge_base_ids.indexOf(id)
  if (index >= 0) form.knowledge_base_ids.splice(index, 1)
  else form.knowledge_base_ids.push(id)
}
async function load(): Promise<void> {
  loading.value = true; error.value = ''
  try {
    const [toolList, kbList] = await Promise.all([
      listTools(),
      listKnowledgeBases().catch(() => [] as KnowledgeBase[]),
    ])
    tools.value = toolList; knowledgeBases.value = kbList
    if (skillId.value) {
      populate(await getSkill(skillId.value))
      versions.value = await listSkillVersions(skillId.value)
    }
  } catch (value) { showError(value) }
  finally { loading.value = false }
}
async function save(): Promise<void> {
  if (saving.value || !canEdit.value) return
  if (!form.key.trim() || !form.name.trim() || !form.content_md.trim()) { error.value = '请填写 key、名称和技能正文。'; return }
  saving.value = true; error.value = ''; notice.value = ''
  try {
    const saved = skillId.value ? await updateSkill(skillId.value, { ...form }) : await createSkill({ ...form })
    populate(saved)
    notice.value = skillId.value ? `已保存新版本 v${saved.current_version}（草稿）。` : `已创建技能 v${saved.current_version}（草稿）。`
    if (!skillId.value) await router.replace('/capabilities/skills/' + saved.id)
    else versions.value = await listSkillVersions(skillId.value)
  } catch (value) { showError(value) }
  finally { saving.value = false }
}
async function publish(): Promise<void> {
  if (!skillId.value || !skill.value || publishing.value) return
  publishing.value = true; error.value = ''; notice.value = ''
  try {
    const saved = await publishSkill(skillId.value, skill.value.current_version)
    populate(saved)
    notice.value = `已发布 v${saved.current_version}。绑定该技能的 Agent 可以重新绑定到新版本。`
    versions.value = await listSkillVersions(skillId.value)
  } catch (value) { showError(value) }
  finally { publishing.value = false }
}
onMounted(load)
</script>

<template>
  <section class="skill-editor">
    <header class="skill-editor__heading">
      <div>
        <button
          type="button"
          class="skill-editor__back"
          @click="router.push('/capabilities?tab=skills')"
        >
          ← 返回能力中心
        </button>
        <span class="skill-editor__eyebrow">SKILL / SOP</span>
        <h1>{{ skillId ? form.name || '技能配置' : '新建技能' }}</h1>
        <p>技能是版本化的工作方法：目标、步骤、示例和输出规范。它不能新增工具权限，只能收窄知识库范围。</p>
      </div>
      <div class="skill-editor__actions">
        <button
          type="button"
          class="skill-editor__primary"
          :disabled="saving || !canEdit"
          @click="save"
        >
          {{ saving ? '正在保存…' : '保存草稿' }}
        </button>
        <button
          v-if="skillId && skill?.status === 'draft'"
          type="button"
          class="skill-editor__publish"
          :disabled="publishing || !canEdit"
          @click="publish"
        >
          {{ publishing ? '正在发布…' : '发布当前版本' }}
        </button>
      </div>
    </header>
    <p
      v-if="error"
      class="skill-editor__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="skill-editor__notice"
    >
      {{ notice }}
    </p>
    <p
      v-if="loading"
      class="skill-editor__hint"
    >
      正在读取技能…
    </p>
    <p
      v-else-if="!canEdit"
      class="skill-editor__hint"
    >
      你可以查看该技能，但只有创建者或空间管理员可以修改。
    </p>

    <div
      v-if="!loading"
      class="skill-editor__layout"
    >
      <div class="skill-editor__main">
        <section class="skill-editor__card">
          <h2>基本信息</h2>
          <div class="skill-editor__fields">
            <label>唯一 key<input
              v-model="form.key"
              :disabled="!!skillId || !canEdit"
              maxlength="64"
              placeholder="policy_qa"
            /></label>
            <label>名称<input
              v-model="form.name"
              :disabled="!canEdit"
              maxlength="100"
            /></label>
            <label class="skill-editor__wide">描述<textarea
              v-model="form.description"
              :disabled="!canEdit"
              rows="2"
              maxlength="2000"
            /></label>
            <label>可见性<select
              v-model="form.visibility"
              :disabled="!canEdit"
            ><option value="private">私有</option><option value="workspace">空间共享</option></select></label>
            <label>默认激活<select
              v-model="form.activation"
              :disabled="!canEdit"
            ><option value="always">常驻提示词</option><option value="on_demand">按需 load_skill</option></select></label>
            <label>token 预算<input
              v-model.number="form.token_budget"
              type="number"
              min="200"
              max="20000"
              :disabled="!canEdit"
            /></label>
            <label>变更说明<input
              v-model="form.change_note"
              :disabled="!canEdit"
              maxlength="500"
            /></label>
          </div>
        </section>

        <section class="skill-editor__card">
          <h2>Markdown / SOP</h2>
          <p class="skill-editor__hint">
            建议包含：适用场景、步骤、判断规则、输出格式、示例。正文会作为工作指令注入 Agent。
          </p>
          <div class="skill-editor__editor">
            <textarea
              v-model="form.content_md"
              :disabled="!canEdit"
              rows="18"
              maxlength="20000"
              placeholder="# 制度问答&#10;&#10;## 步骤&#10;1. 检索知识库&#10;2. 给出带来源的结论"
            />
            <div class="skill-editor__preview">
              <span>预览</span>
              <MarkdownBody :content="form.content_md || '（暂无正文）'" />
            </div>
          </div>
        </section>

        <section class="skill-editor__card">
          <h2>声明的工具与知识库</h2>
          <p class="skill-editor__hint">
            声明的工具必须是绑定 Agent 已启用的工具；知识库范围只会收窄，不会扩大权限。
          </p>
          <div class="skill-editor__options">
            <label
              v-for="tool in tools"
              :key="tool.key"
              class="skill-editor__option"
            >
              <input
                type="checkbox"
                :checked="form.tool_keys.includes(tool.key)"
                :disabled="!canEdit"
                @change="toggleTool(tool.key)"
              />
              <span><strong>{{ tool.display_name }}</strong><small>{{ riskLabels[tool.risk] ?? tool.risk }}</small></span>
            </label>
          </div>
          <label class="skill-editor__radio"><input
            v-model="form.knowledge_mode"
            type="radio"
            value="all_readable"
            :disabled="!canEdit"
          /> 全部可读知识库</label>
          <label class="skill-editor__radio"><input
            v-model="form.knowledge_mode"
            type="radio"
            value="selected"
            :disabled="!canEdit"
          /> 指定知识库</label>
          <div
            v-if="form.knowledge_mode === 'selected'"
            class="skill-editor__kb-list"
          >
            <label
              v-for="kb in knowledgeBases"
              :key="kb.id"
            ><input
              type="checkbox"
              :checked="form.knowledge_base_ids.includes(kb.id)"
              :disabled="!canEdit"
              @change="toggleKnowledge(kb.id)"
            /> {{ kb.name }}</label>
          </div>
        </section>
      </div>

      <aside class="skill-editor__side">
        <section class="skill-editor__card">
          <h2>版本</h2>
          <p class="skill-editor__hint">
            当前 v{{ skill?.current_version ?? 1 }} · {{ skill?.status === 'published' ? '已发布' : '草稿' }}
          </p>
          <ol class="skill-editor__versions">
            <li
              v-for="item in versions"
              :key="item.id"
            >
              <strong>v{{ item.version }}</strong>
              <span>{{ item.published_at ? '已发布' : '草稿' }} · {{ item.change_note || '无说明' }}</span>
            </li>
          </ol>
          <p
            v-if="!versions.length"
            class="skill-editor__hint"
          >
            保存后显示版本历史。
          </p>
        </section>
        <section class="skill-editor__card">
          <h2>边界</h2>
          <p class="skill-editor__hint">
            技能不执行代码、不访问网络、不新增权限。运行时只会把正文注入模型上下文，或由 load_skill 按需加载。
          </p>
        </section>
      </aside>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
.skill-editor {
  max-width: 1280px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; padding-bottom: 22px; border-bottom: 1px solid $line; h1 { font-size: clamp(26px, 3vw, 38px); font-weight: 400; margin: 8px 0; } p { color: $muted; font-size: 12px; line-height: 1.9; max-width: 680px; } }
  &__back { border: 0; background: none; color: $green; font-size: 12px; padding: 0 0 12px; }
  &__eyebrow { font-size: 9px; letter-spacing: 2px; color: #859576; }
  &__actions { display: flex; gap: 9px; }
  &__primary, &__publish { border: 0; border-radius: 5px; padding: 10px 16px; font-size: 12px; white-space: nowrap; }
  &__primary { background: #31533d; color: #edf4e4; }
  &__publish { background: #e8eedd; color: $green; border: 1px solid #cbd7be; }
  &__layout { display: grid; grid-template-columns: minmax(0, 1fr) 290px; gap: 18px; margin-top: 24px; align-items: start; }
  &__main { display: grid; gap: 18px; }
  &__side { display: grid; gap: 18px; position: sticky; top: 20px; }
  &__card { border: 1px solid $line; background: #fff; border-radius: 8px; padding: 20px; h2 { font-size: 14px; color: $green; margin-bottom: 14px; font-weight: 500; } }
  &__fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; label { font-size: 11px; color: $green; display: flex; flex-direction: column; gap: 6px; } input, select, textarea { border: 1px solid #d6dfd0; background: $paper; border-radius: 4px; padding: 8px; font: inherit; color: $ink; } }
  &__wide { grid-column: 1 / -1; }
  &__editor { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; textarea { width: 100%; border: 1px solid #d6dfd0; background: $paper; border-radius: 5px; padding: 12px; font: 12px/1.8 ui-monospace, SFMono-Regular, Consolas, monospace; color: $ink; resize: vertical; } }
  &__preview { border: 1px solid #e0e6da; border-radius: 5px; padding: 12px; max-height: 430px; overflow: auto; background: #fcfdf9; > span { display: block; color: $muted; font-size: 10px; margin-bottom: 8px; } }
  &__options { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
  &__option { display: flex; gap: 8px; align-items: center; border: 1px solid #e0e6da; border-radius: 5px; padding: 9px; font-size: 12px; span { display: flex; flex-direction: column; } small { color: $muted; font-size: 10px; } }
  &__radio { display: block; font-size: 12px; margin: 9px 0; }
  &__kb-list { display: grid; gap: 6px; font-size: 12px; }
  &__hint { color: $muted; font-size: 11px; line-height: 1.8; }
  &__versions { list-style: none; padding: 0; margin: 0; display: grid; gap: 9px; li { display: flex; flex-direction: column; gap: 3px; border-bottom: 1px solid #edf0e9; padding-bottom: 8px; font-size: 11px; } span { color: $muted; font-size: 10px; } }
  &__error { color: #8b493c; background: #faf0e8; border: 1px solid #e8d7c7; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-top: 18px; }
  &__notice { color: #386b4f; background: #edf4e6; border: 1px solid #d6e5c9; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-top: 18px; }
}
@media (max-width: 900px) { .skill-editor__layout { grid-template-columns: minmax(0, 1fr); } .skill-editor__side { position: static; } .skill-editor__editor { grid-template-columns: minmax(0, 1fr); } .skill-editor__heading { flex-direction: column; align-items: flex-start; } }
</style>
