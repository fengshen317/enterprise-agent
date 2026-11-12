<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { createMember, listAudits, listMembers, updateMember, type Audit, type Member, type MemberUpdate, type Role } from '../api/admin'
import { useAuthStore } from '../stores/auth'
import { listConfigs, updateConfigs, type ConfigItem, type ConfigUpdate } from '../api/config'

const auth = useAuthStore()
const router = useRouter()
const allowed = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
const members = ref<Member[]>([])
const audits = ref<Audit[]>([])
const drafts = ref<Record<string, MemberUpdate>>({})
const total = ref(0)
const offset = ref(0)
const configs = ref<ConfigItem[]>([])
const configDrafts = ref<Record<string, string>>({})
const configSaving = ref('')
const configNotice = ref('')
const auditOffset = ref(0)
const query = ref('')
const appliedQuery = ref('')
const loading = ref(false)
const saving = ref('')
const creating = ref(false)
const showCreate = ref(false)
const error = ref('')
const notice = ref('')
const username = ref('')
const displayName = ref('')
const password = ref('')
const role = ref<Role>('member')
const PAGE_SIZE = 20
let alive = true
let revision = 0

async function handleError(value: unknown): Promise<void> {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
  if (value instanceof ApiError && value.status === 403) {
    members.value = []; audits.value = []; drafts.value = {}
    try {
      await auth.restore()
    } catch { /* 保留原始权限错误，避免显示旧管理数据。 */ }
    if (!allowed.value) await router.replace('/chat')
  }
}
async function load(): Promise<void> {
  if (!allowed.value) return
  const token = ++revision
  loading.value = true; error.value = ''
  configs.value = await listConfigs().catch(() => [] as ConfigItem[])
  try {
    const [page, history] = await Promise.all([listMembers(offset.value, appliedQuery.value), listAudits(auditOffset.value)])
    if (!alive || token !== revision) return
    members.value = page.items; total.value = page.total; audits.value = history
    drafts.value = Object.fromEntries(page.items.map(item => [item.user_id, { role: item.role, status: item.status }]))
  } catch (value) { if (alive && token === revision) await handleError(value) }
  finally { if (alive && token === revision) loading.value = false }
}
async function search(): Promise<void> {
  appliedQuery.value = query.value; offset.value = 0
  await load()
}
async function create(): Promise<void> {
  if (creating.value) return
  creating.value = true; error.value = ''; notice.value = ''
  try {
    await createMember({ username: username.value, display_name: displayName.value, password: password.value, role: role.value })
    if (!alive) return
    username.value = ''; displayName.value = ''; password.value = ''; role.value = 'member'; showCreate.value = false
    query.value = ''; appliedQuery.value = ''; offset.value = 0; auditOffset.value = 0
    notice.value = '用户已创建，可使用设置的密码登录。'
    await load()
  } catch (value) { if (alive) await handleError(value) }
  finally { creating.value = false }
}
async function save(item: Member): Promise<void> {
  const body = drafts.value[item.user_id]
  if (!body || saving.value) return
  saving.value = item.user_id; error.value = ''; notice.value = ''
  try {
    await updateMember(item.user_id, { ...body })
    if (!alive) return
    if (item.user_id === auth.session?.user.id) {
      await auth.restore()
      if (!allowed.value) { await router.replace('/chat'); return }
    }
    notice.value = '成员权限已更新。'; auditOffset.value = 0
    await load()
  } catch (value) { if (alive) await handleError(value) }
  finally { saving.value = '' }
}
function dirty(item: Member): boolean {
  const draft = drafts.value[item.user_id]
  return !!draft && (draft.role !== item.role || draft.status !== item.status)
}
async function page(delta: number): Promise<void> { offset.value = Math.max(0, offset.value + delta * PAGE_SIZE); await load() }
async function auditPage(delta: number): Promise<void> { auditOffset.value = Math.max(0, auditOffset.value + delta * PAGE_SIZE); await load() }
function configDraft(key: string): string {
  return configDrafts.value[key] ?? ''
}
function setConfigDraft(key: string, value: string): void {
  configDrafts.value[key] = value
}
async function saveConfig(keys: string[]): Promise<void> {
  const updates: ConfigUpdate[] = keys
    .filter(key => configDrafts.value[key] !== undefined && configDrafts.value[key] !== '')
    .map(key => ({ key, value: configDrafts.value[key] }))
  if (!updates.length) return
  configSaving.value = keys.join(',')
  configNotice.value = ''
  try {
    configs.value = await updateConfigs(updates)
    configDrafts.value = {}
    configNotice.value = '配置已保存并生效。'
  } catch (value) {
    configNotice.value = value instanceof Error ? value.message : '保存失败'
  } finally {
    configSaving.value = ''
  }
}
function roleName(value: string | null): string { return value === 'admin' ? '管理员' : value === 'member' ? '普通成员' : '无' }
function stateName(value: string | null): string { return value === 'active' ? '启用' : value === 'revoked' ? '停用' : '无' }
function timeLabel(value: string): string { return new Date(value).toLocaleString('zh-CN') }
void load()
onBeforeUnmount(() => { alive = false; revision++; password.value = '' })
</script>

<template>
  <section class="admin">
    <header class="admin__heading">
      <div>
        <p class="admin__eyebrow">
          WORKSPACE / ADMINISTRATION
        </p><h1>成员与权限</h1><p class="admin__description">
          {{ auth.activeWorkspace?.name }} · 管理当前空间的访问权限
        </p>
      </div>
      <button
        v-if="allowed"
        class="admin__primary"
        type="button"
        :disabled="loading || creating || !!saving"
        @click="showCreate = !showCreate"
      >
        {{ showCreate ? '收起表单' : '＋ 添加用户' }}
      </button>
    </header>
    <p
      v-if="!allowed"
      class="admin__error"
      role="alert"
    >
      仅当前工作空间的管理员可访问此页面。
    </p>
    <template v-else>
      <p class="admin__boundary">
        管理员可以管理成员和角色，不能查看其他成员的私有对话。停用只影响当前空间。
      </p>
      <p
        v-if="error"
        class="admin__error"
        role="alert"
      >
        {{ error }}
      </p>
      <p
        v-if="notice"
        class="admin__notice"
        role="status"
      >
        {{ notice }}
      </p>
      <form
        v-if="showCreate"
        class="admin__create"
        @submit.prevent="create"
      >
        <h2>创建用户</h2>
        <div class="admin__fields">
          <label>登录账号<input
            v-model="username"
            name="username"
            maxlength="64"
            required
            :disabled="creating"
            placeholder="如 alice（3–64 位字母、数字、_ . -）"
          /></label>
          <label>显示名<input
            v-model="displayName"
            name="displayName"
            maxlength="100"
            required
            :disabled="creating"
            placeholder="输入成员姓名"
          /></label>
          <label>初始密码<input
            v-model="password"
            name="newPassword"
            type="text"
            autocomplete="off"
            :disabled="creating"
            placeholder="明文显示，不限制长度或字符"
          /></label>
          <label>空间角色<select
            v-model="role"
            name="newRole"
            :disabled="creating"
          ><option value="member">普通成员</option><option value="admin">管理员</option></select></label>
        </div>
        <p class="admin__hint">
          新用户加入当前空间。已有同名账号不会被覆盖，密码不会保存在管理记录中。
        </p>
        <button
          class="admin__primary"
          type="submit"
          :disabled="creating || !username.trim() || !displayName.trim()"
        >
          {{ creating ? '正在创建…' : '创建用户' }}
        </button>
      </form>
      <section
        class="admin__card"
        aria-label="成员列表"
      >
        <form
          class="admin__toolbar"
          @submit.prevent="search"
        >
          <label for="member-search">空间成员 <span>{{ total }}</span></label>
          <div>
            <input
              id="member-search"
              v-model="query"
              maxlength="100"
              placeholder="搜索账号或显示名"
            /><button
              type="submit"
              :disabled="loading"
            >
              搜索
            </button><button
              type="button"
              :disabled="loading"
              @click="load"
            >
              刷新
            </button>
          </div>
        </form>
        <div
          class="admin__table-wrap"
          :aria-busy="loading"
        >
          <table class="admin__table">
            <thead><tr><th>成员</th><th>空间角色</th><th>空间访问</th><th>操作</th></tr></thead>
            <tbody>
              <tr
                v-for="item in members"
                :key="item.user_id"
              >
                <td><strong>{{ item.display_name }}</strong><small>{{ item.username }}{{ item.user_id === auth.session?.user.id ? ' · 你' : '' }}{{ item.user_status === 'disabled' ? ' · 账号已禁用' : '' }}</small></td>
                <td>
                  <select
                    v-if="drafts[item.user_id]"
                    v-model="drafts[item.user_id]!.role"
                    :aria-label="item.username + '的角色'"
                    :disabled="loading || !!saving"
                  >
                    <option value="member">
                      普通成员
                    </option><option value="admin">
                      管理员
                    </option>
                  </select>
                </td>
                <td>
                  <select
                    v-if="drafts[item.user_id]"
                    v-model="drafts[item.user_id]!.status"
                    :aria-label="item.username + '的空间访问'"
                    :disabled="loading || !!saving"
                  >
                    <option value="active">
                      启用
                    </option><option value="revoked">
                      停用
                    </option>
                  </select>
                </td>
                <td>
                  <button
                    type="button"
                    class="admin__save"
                    :disabled="loading || !!saving || !dirty(item)"
                    @click="save(item)"
                  >
                    {{ saving === item.user_id ? '保存中…' : '保存权限' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <p
            v-if="!members.length"
            class="admin__empty"
          >
            {{ loading ? '正在读取成员…' : '没有符合条件的成员' }}
          </p>
        </div>
        <div class="admin__pagination">
          <span>第 {{ Math.floor(offset / PAGE_SIZE) + 1 }} 页 · 共 {{ total }} 位成员</span><div>
            <button
              type="button"
              :disabled="loading || offset === 0"
              @click="page(-1)"
            >
              上一页
            </button><button
              type="button"
              :disabled="loading || offset + PAGE_SIZE >= total"
              @click="page(1)"
            >
              下一页
            </button>
          </div>
        </div>
      </section>
      <section
        class="admin__card"
        aria-label="管理操作记录"
      >
        <div class="admin__audit-heading">
          <h2>操作记录</h2><span>只记录当前空间的实际变更</span>
        </div>
        <ol class="admin__audits">
          <li
            v-for="item in audits"
            :key="item.id"
          >
            <p><strong>{{ item.actor_username }}</strong> {{ item.action === 'create_user' ? '创建了' : '更新了' }} <strong>{{ item.target_username }}</strong></p><p>{{ roleName(item.before_role) }} / {{ stateName(item.before_status) }} → {{ roleName(item.after_role) }} / {{ stateName(item.after_status) }}</p><time>{{ timeLabel(item.created_at) }}</time>
          </li>
        </ol>
        <p
          v-if="!audits.length"
          class="admin__empty"
        >
          暂无管理操作记录。
        </p>
        <div class="admin__pagination">
          <span>最近操作 · 第 {{ Math.floor(auditOffset / PAGE_SIZE) + 1 }} 页</span><div>
            <button
              type="button"
              :disabled="loading || auditOffset === 0"
              @click="auditPage(-1)"
            >
              上一页
            </button><button
              type="button"
              :disabled="loading || audits.length < PAGE_SIZE"
              @click="auditPage(1)"
            >
              下一页
            </button>
          </div>
        </div>
      </section>
      <section
        class="admin__card"
        aria-label="集成配置"
      >
        <div class="admin__audit-heading">
          <h2>集成配置</h2><span>模型与联网搜索参数，保存即生效</span>
        </div>
        <p
          v-if="configNotice"
          class="admin__notice"
          role="status"
        >
          {{ configNotice }}
        </p>
        <div class="admin__config-groups">
          <div class="admin__config-group">
            <h3>模型配置（DeepSeek）</h3>
            <div class="admin__fields">
              <label>API Key
                <input
                  type="password"
                  :placeholder="configs.find(c => c.key === 'deepseek_api_key')?.value_masked || '未配置'"
                  :value="configDraft('deepseek_api_key')"
                  @input="setConfigDraft('deepseek_api_key', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label>模型名称
                <input
                  :value="configDraft('deepseek_model') || configs.find(c => c.key === 'deepseek_model')?.value_masked"
                  @input="setConfigDraft('deepseek_model', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label>超时（秒）
                <input
                  type="number"
                  :value="configDraft('model_timeout_seconds') || configs.find(c => c.key === 'model_timeout_seconds')?.value_masked"
                  @input="setConfigDraft('model_timeout_seconds', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label>最大 Token
                <input
                  type="number"
                  :value="configDraft('model_max_tokens') || configs.find(c => c.key === 'model_max_tokens')?.value_masked"
                  @input="setConfigDraft('model_max_tokens', ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
            <button
              type="button"
              class="admin__primary"
              :disabled="configSaving !== ''"
              @click="saveConfig(['deepseek_api_key', 'deepseek_model', 'model_timeout_seconds', 'model_max_tokens'])"
            >
              保存模型配置
            </button>
          </div>
          <div class="admin__config-group">
            <h3>联网搜索（Tavily）</h3>
            <div class="admin__fields">
              <label>API Key
                <input
                  type="password"
                  :placeholder="configs.find(c => c.key === 'tavily_api_key')?.value_masked || '未配置'"
                  :value="configDraft('tavily_api_key')"
                  @input="setConfigDraft('tavily_api_key', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label>超时（秒）
                <input
                  type="number"
                  :value="configDraft('web_search_timeout_seconds') || configs.find(c => c.key === 'web_search_timeout_seconds')?.value_masked"
                  @input="setConfigDraft('web_search_timeout_seconds', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label>返回条数
                <input
                  type="number"
                  :value="configDraft('web_search_max_results') || configs.find(c => c.key === 'web_search_max_results')?.value_masked"
                  @input="setConfigDraft('web_search_max_results', ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
            <button
              type="button"
              class="admin__primary"
              :disabled="configSaving !== ''"
              @click="saveConfig(['tavily_api_key', 'web_search_timeout_seconds', 'web_search_max_results'])"
            >
              保存搜索配置
            </button>
          </div>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.admin {
  max-width: 1200px; margin: 0 auto; padding: 40px clamp(20px, 4vw, 56px) 70px;
  &__heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 22px; h1 { font-size: 28px; font-weight: 500; margin: 13px 0; } }
  &__eyebrow { color: $muted; font-size: 10px; letter-spacing: 2px; }
  &__description, &__hint, &__boundary { color: $muted; font-size: 12px; line-height: 1.9; }
  &__boundary { border-left: 2px solid $line; padding-left: 14px; margin-bottom: 26px; }
  &__primary { background: $green; color: white; border: 0; border-radius: 6px; padding: 11px 17px; }
  &__create, &__card { margin: 22px 0; background: white; border: 1px solid $line; border-radius: 8px; overflow: hidden; }
  &__create { padding: 24px; h2 { margin-bottom: 20px; font-size: 17px; font-weight: 500; } .admin__hint { margin: 16px 0; } }
  &__fields { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; label { color: $green; font-size: 12px; } input, select { display: block; margin-top: 8px; width: 100%; } }
  input, select { border: 1px solid $line; border-radius: 5px; padding: 9px 11px; background: $paper; color: $ink; font: inherit; font-size: 12px; &:focus { outline: 2px solid #92ae7a; outline-offset: 2px; } }
  &__toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 20px; label { font-size: 14px; span { color: $muted; margin-left: 6px; } } div { display: flex; flex-wrap: wrap; gap: 8px; } }
  &__toolbar button, &__pagination button, &__save { border: 1px solid $line; background: $paper; border-radius: 5px; padding: 8px 12px; color: $green; font-size: 12px; }
  &__table-wrap { overflow-x: auto; }
  &__table { width: 100%; border-collapse: collapse; text-align: left; th { background: $paper; color: $muted; font-size: 11px; font-weight: 400; padding: 13px 20px; white-space: nowrap; } td { padding: 17px 20px; border-bottom: 1px solid $line; font-size: 12px; white-space: nowrap; } strong { font-weight: 500; } small { display: block; color: $muted; margin-top: 7px; } }
  &__config-groups { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 16px; }
  &__config-group { h3 { font-size: 13px; color: $green; margin-bottom: 12px; } button { margin-top: 14px; } }
  &__pagination { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 16px 20px; color: $muted; font-size: 11px; div { display: flex; gap: 8px; } }
  &__error, &__notice { padding: 12px 16px; border-radius: 5px; font-size: 12px; line-height: 1.8; }
  &__error { background: #faeee6; color: #934b40; }
  &__notice { background: #eaf0e1; color: $green; }
  &__empty { padding: 24px; color: $muted; font-size: 12px; }
  &__audit-heading { padding: 20px; display: flex; gap: 16px; align-items: center; h2 { font-size: 15px; font-weight: 500; } span { color: $muted; font-size: 11px; } }
  &__audits { list-style: none; padding: 0 20px; li { border-top: 1px solid $line; padding: 15px 0; display: grid; gap: 7px; font-size: 12px; line-height: 1.8; } strong { font-weight: 500; color: $green; } time { color: $muted; font-size: 10px; } }
}
@media (max-width: 760px) { .admin { padding: 24px 16px; &__fields { grid-template-columns: 1fr; } &__heading { align-items: flex-start; flex-direction: column; } &__table td, &__table th { padding: 14px 12px; } } }
</style>
