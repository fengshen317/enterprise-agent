<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  createBindCode,
  listEnterpriseApps,
  loadEnterpriseBinding,
  saveEnterpriseApp,
  unbindEnterprise,
  type BindCode,
  type EnterpriseApp,
  type EnterpriseBinding,
  type EnterpriseChannel,
} from '../../api/channels'
import { useAuthStore } from '../../stores/auth'

const props = defineProps<{ channel: EnterpriseChannel }>()

const copy: Record<EnterpriseChannel, { title: string, lead: string, limits: string[] }> = {
  feishu: {
    title: '飞书',
    lead: '使用企业自建应用和机器人能力。员工在单聊里发送绑定码后，消息才会进入自己的工作台账号。',
    limits: [
      '只处理单聊。群消息会记下来，但不会进入问答。',
      '飞书没有 24 小时回复窗口。可用范围内的员工可以接收主动推送。',
      '同一用户发送大约限制在 5 次/秒。不在可用范围时，页面和日志会提示调整可见范围，而不是反复重试。',
      '长连接是集群模式：同一应用只有一个进程会收到事件。保存新凭证后会重新连接。',
    ],
  },
  dingtalk: {
    title: '钉钉',
    lead: '使用企业内部应用机器人，不用群自定义机器人。员工先在钉钉里打开与机器人的单聊，定时结果才推得出去。',
    limits: [
      '只处理单聊（conversationType=1）。',
      '主动推送走批量单聊接口，不用会过期的 sessionWebhook。',
      '还没打开过单聊时，钉钉会返回「没有有效的机器人单聊会话」。不允许主动发单聊、限流员工名单也按配置问题处理，不会自动重试。',
      '标准版付费 API 大约每月 1 万次、20 QPS。AI 基础版及以上不设自然月上限，但仍有 QPS 限制。是否计入免费接口以开发者后台资源管理和豁免清单为准。',
    ],
  },
  qq: {
    title: 'QQ',
    lead: '只使用 QQ 官方机器人开放接口。这和能力中心里的 QQ 邮箱不是同一个东西。',
    limits: [
      '只处理单聊。用户需要先加机器人为好友。',
      '被动回复窗口是 60 分钟，每条消息最多回复 4 次。处理过程用「正在输入」，避免进度把次数用完。',
      '主动消息需要用户没有关闭「允许主动发送」。企业认证或个人认证大约 10 次/秒，每个好友每天最多约 1000 条。',
      '个人主体机器人不能公开，私聊测试最多 20 个号码。给团队用需要企业认证。',
    ],
  },
}

const auth = useAuthStore()
const isAdmin = computed(() => auth.activeWorkspace?.role === 'admin' && auth.activeWorkspace.status === 'active')
const page = computed(() => copy[props.channel])
const app = ref<EnterpriseApp | null>(null)
const binding = ref<EnterpriseBinding | null>(null)
const code = ref<BindCode | null>(null)
const externalId = ref('')
const secret = ref('')
const robotCode = ref('')
const sandbox = ref(false)
const error = ref('')
const notice = ref('')
const loading = ref(false)
const busy = ref(false)

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [apps, current] = await Promise.all([
      listEnterpriseApps(),
      loadEnterpriseBinding(props.channel),
    ])
    app.value = apps.find(item => item.channel_type === props.channel) ?? null
    binding.value = current
    externalId.value = app.value?.external_app_id ?? ''
    robotCode.value = app.value?.robot_code ?? ''
    sandbox.value = app.value?.sandbox ?? false
    secret.value = ''
  } catch (value) {
    showError(value)
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    app.value = await saveEnterpriseApp({
      channel_type: props.channel,
      external_app_id: externalId.value.trim(),
      secret: secret.value,
      robot_code: props.channel === 'dingtalk' ? robotCode.value.trim() : '',
      sandbox: props.channel === 'qq' ? sandbox.value : false,
    })
    secret.value = ''
    notice.value = '已保存。正在使用的连接会改用这份凭证。'
    await load()
  } catch (value) {
    showError(value)
  } finally {
    busy.value = false
  }
}

async function issue(): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    code.value = await createBindCode(props.channel)
  } catch (value) {
    showError(value)
  } finally {
    busy.value = false
  }
}

async function unbind(): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await unbindEnterprise(props.channel)
    code.value = null
    notice.value = '已解除绑定。'
    await load()
  } catch (value) {
    showError(value)
  } finally {
    busy.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <section class="enterprise">
    <header class="enterprise__header">
      <h1>{{ page.title }}</h1>
      <p>{{ page.lead }}</p>
    </header>
    <p
      v-if="error"
      class="enterprise__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="enterprise__notice"
    >
      {{ notice }}
    </p>
    <p v-if="loading">
      正在读取渠道状态…
    </p>
    <form
      v-else-if="isAdmin"
      class="enterprise__panel"
      @submit.prevent="save"
    >
      <h2>机器人配置</h2>
      <p v-if="app?.secret_configured">
        密钥已保存。留空表示不修改。
      </p>
      <p v-else>
        还没有可用密钥。
      </p>
      <label>
        {{ channel === 'dingtalk' ? 'Client ID / AppKey' : 'App ID' }}
        <input
          v-model="externalId"
          name="externalAppId"
          required
          maxlength="128"
          autocomplete="off"
        />
      </label>
      <label>
        {{ channel === 'qq' ? 'Client Secret' : 'App Secret' }}
        <input
          v-model="secret"
          name="appSecret"
          type="password"
          maxlength="512"
          autocomplete="new-password"
        />
      </label>
      <label v-if="channel === 'dingtalk'">
        Robot Code
        <input
          v-model="robotCode"
          name="robotCode"
          maxlength="128"
          autocomplete="off"
          placeholder="不填时使用 AppKey"
        />
      </label>
      <label
        v-if="channel === 'qq'"
        class="enterprise__check"
      >
        <input
          v-model="sandbox"
          name="qqSandbox"
          type="checkbox"
        />
        使用沙箱环境
      </label>
      <button
        type="submit"
        :disabled="busy"
      >
        保存配置
      </button>
    </form>
    <section
      v-if="!loading"
      class="enterprise__panel"
    >
      <h2>我的绑定</h2>
      <p v-if="!binding?.app_configured">
        管理员还没有启用这个机器人。
      </p>
      <p v-else-if="binding.bound">
        已绑定平台用户 {{ binding.platform_user_id || '未知' }}。要换成另一个账号，请先解除绑定。
      </p>
      <p v-else>
        还没有绑定。生成绑定码后，在和机器人的单聊里发送它。
      </p>
      <div class="enterprise__actions">
        <button
          type="button"
          :disabled="busy || !binding?.app_configured || binding.bound"
          @click="issue"
        >
          生成绑定码
        </button>
        <button
          v-if="binding?.bound"
          type="button"
          :disabled="busy"
          @click="unbind"
        >
          解除绑定
        </button>
      </div>
      <p
        v-if="code"
        class="enterprise__code"
      >
        <strong>{{ code.code }}</strong>
        <span>{{ code.instructions }}</span>
      </p>
    </section>
    <aside class="enterprise__help">
      <p
        v-for="line in page.limits"
        :key="line"
      >
        {{ line }}
      </p>
    </aside>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.enterprise {
  display: grid;
  gap: 14px;
  max-width: 1180px;
  margin: 0 auto;
  padding: 34px clamp(16px, 4vw, 52px) 48px;

  &__header h1 { @include page-title; margin-bottom: 7px; }
  &__header p, &__help { color: $muted; font-size: 13px; }
  &__error { padding: 9px 11px; border-radius: $control-radius; color: $log-error; background: $log-error-bg; }
  &__notice { padding: 9px 11px; border-radius: $control-radius; background: $sidebar; }
  &__panel, &__help {
    display: grid;
    gap: 10px;
    padding: 16px;
    border: 1px solid $line;
    border-radius: $panel-radius;
    background: $surface;
  }
  &__help { border: 0; background: $sidebar; }
  h2 { margin: 0; font-size: 1.05rem; }
  label { display: grid; gap: 6px; max-width: 28rem; font-size: 13px; }
  input[type='text'], input:not([type]), input[type='password'] {
    min-width: 0;
    padding: 8px 10px;
    border: 1px solid $line;
    border-radius: $control-radius;
    background: $paper;
  }
  &__check { display: flex; align-items: center; gap: 8px; }
  button {
    justify-self: start;
    min-height: 36px;
    border: 1px solid $green;
    border-radius: $control-radius;
    padding: 7px 12px;
    background: $green;
    color: $on-green;
    font-weight: 650;
    &:enabled:hover { background: $green-hover; }
  }
  &__actions { display: flex; flex-wrap: wrap; gap: 8px; }
  &__actions button:last-child:not(:only-child) {
    border-color: $line;
    background: $surface;
    color: $log-error;
  }
  &__code {
    display: grid;
    gap: 4px;
    strong { font-size: 1.3rem; letter-spacing: .08em; }
    span { color: $muted; }
  }
}
</style>
