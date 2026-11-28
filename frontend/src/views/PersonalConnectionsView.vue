<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { listWeChatAccounts } from '../api/channels'
import { ApiError } from '../api/client'
import { getConnector, type ConnectorDetail, type PersonalAccount } from '../api/connectors'
import { listPersonalModels } from '../api/personalModels'
import PersonalConnectorAccount from './connectors/PersonalConnectorAccount.vue'

const route = useRoute()
const mail = ref<ConnectorDetail | null>(null)
const mailOpen = ref(false)
const mailError = ref('')
const wechatStatus = ref('正在读取…')
const modelStatus = ref('正在读取…')
const mailStatus = computed(() => {
  if (!mail.value) return mailError.value || '正在读取…'
  if (!mail.value.enabled || !mail.value.personal_account) return '当前工作空间未开放'
  return mail.value.personal_account.bound ? '已绑定' : '尚未绑定'
})
const conversation = computed(() => typeof route.query.conversation === 'string' ? route.query.conversation : '')
const chatTarget = computed(() => ({
  name: 'chat', params: { conversationId: conversation.value }, query: { plugin: 'qq_mail' },
}))

async function loadMail(): Promise<void> {
  mailError.value = ''
  try { mail.value = await getConnector('qq_mail') }
  catch (error) {
    mailError.value = error instanceof ApiError && error.status === 404 ? '当前工作空间未开放，请联系空间管理员'
      : error instanceof Error ? error.message : '读取邮箱状态失败'
  }
}

async function loadWeChat(): Promise<void> {
  try {
    const accounts = await listWeChatAccounts()
    wechatStatus.value = accounts.length ? '已绑定 ' + accounts.length + ' 个微信账号' : '尚未绑定'
  } catch { wechatStatus.value = '暂时无法读取绑定状态' }
}

function updateMail(account: PersonalAccount): void {
  if (mail.value) mail.value = { ...mail.value, personal_account: account }
  mailOpen.value = false
}

async function loadModels(): Promise<void> {
  try {
    const models = await listPersonalModels()
    const personal = models.profiles.find(profile => profile.active)
    modelStatus.value = personal ? '个人默认 · ' + personal.model_id
      : models.shared_default.model_id ? '空间默认 · ' + models.shared_default.model_id : '尚未配置默认模型'
  } catch { modelStatus.value = '暂时无法读取默认模型' }
}

onMounted(() => { void loadMail(); void loadWeChat(); void loadModels() })
</script>

<template>
  <section class="connections">
    <header>
      <h1>我的账号与连接</h1>
      <p>管理你在工作台使用的账号和模型。</p>
    </header>
    <div class="connections__list">
      <section class="connections__item">
        <div class="connections__row">
          <div>
            <h2>微信</h2>
            <p>在微信里与助手对话。</p>
            <span class="connections__status">{{ wechatStatus }}</span>
          </div>
          <RouterLink
            class="connections__action"
            to="/channels/wechat"
          >管理微信</RouterLink>
        </div>
      </section>
      <section class="connections__item">
        <div class="connections__row">
          <div>
            <h2>QQ 邮箱</h2>
            <p>在当前工作空间查询自己收到的邮件。</p>
            <span
              class="connections__status"
              role="status"
            >{{ mailStatus }}</span>
          </div>
          <div class="connections__actions">
            <RouterLink
              v-if="mail?.enabled && mail.personal_account?.bound"
              class="connections__action connections__action--primary"
              :to="chatTarget"
            >{{ conversation ? '回到原对话查会议通知' : '去问答查会议通知' }}</RouterLink>
            <button
              v-if="mail?.personal_account && (mail.enabled || mail.personal_account.bound)"
              class="connections__action"
              type="button"
              :aria-expanded="mailOpen"
              @click="mailOpen = !mailOpen"
            >
              {{ mailOpen ? '收起邮箱设置' : mail.personal_account.bound ? '管理邮箱' : '绑定邮箱' }}
            </button>
            <button
              v-if="mailError"
              class="connections__action"
              type="button"
              @click="loadMail"
            >
              重新加载
            </button>
          </div>
        </div>
        <PersonalConnectorAccount
          v-if="mailOpen && mail?.personal_account"
          class="connections__editor"
          connector-key="qq_mail"
          :account="mail.personal_account"
          :enabled="mail.enabled"
          confirm-before-unbind
          @updated="updateMail"
        />
      </section>
      <section class="connections__item">
        <div class="connections__row">
          <div>
            <h2>我的模型</h2>
            <p>为自己的问答选择模型，设置随账号生效。</p>
            <span class="connections__status">{{ modelStatus }}</span>
          </div>
          <RouterLink
            class="connections__action"
            to="/account/models"
          >管理模型</RouterLink>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.connections {
  max-width: 860px; margin: 0 auto; padding: 34px 24px;
  h1 { @include page-title; margin: 0; }
  h2 { margin: 0; font-size: 18px; }
  p { color: $muted; margin: 6px 0; }
  &__list { margin-top: 24px; border: 1px solid $line; background: $surface; border-radius: $panel-radius; }
  &__item { padding: 20px 24px; }
  &__item + &__item, &__editor { border-top: 1px solid $line; }
  &__row, &__actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
  &__row { justify-content: space-between; }
  &__row > div { min-width: 0; }
  &__status { color: $green; font-size: 13px; overflow-wrap: anywhere; }
  &__action {
    display: inline-block; background: $surface; color: $green; border: 1px solid $line;
    border-radius: $control-radius; padding: 8px 12px; font: inherit; text-decoration: none;
    &:hover { border-color: $green; }
    &--primary { background: $green; color: $on-green; }
  }
  &__editor { margin-top: 16px; padding-top: 16px; }
  @media (max-width: 560px) {
    padding: 24px 16px;
    &__item { padding: 16px; }
    &__row { align-items: flex-start; flex-direction: column; }
  }
}
</style>
