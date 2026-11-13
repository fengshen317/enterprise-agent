<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import {
  listWeChatAccounts,
  pollWeChatBind,
  startWeChatBind,
  unbindWeChat,
  type WeChatAccount,
  type WeChatBindSession,
} from '../../api/channels'

const accounts = ref<WeChatAccount[]>([])
const session = ref<WeChatBindSession | null>(null)
const verifyCode = ref('')
const error = ref('')
const loading = ref(false)
const busy = ref(false)
let timer = 0

const statusText: Record<string, string> = {
  active: '已连接',
  paused: '会话暂停',
  pending: '等待扫码',
  wait: '等待扫码',
  scaned: '已扫码，请在手机确认',
  confirmed: '已绑定',
  expired: '二维码已失效',
  failed: '绑定失败',
}

function showError(value: unknown): void {
  error.value = value instanceof Error ? value.message : '操作失败，请重试'
}

function reasonText(reason: string): string {
  if (reason === 'outside_window') return '已超过 24 小时'
  if (reason === 'no_context_token') return '还没有会话'
  if (reason === 'no_inbound') return '还没有收到消息'
  return reason || '未发送'
}

function imageUrl(content: string): string {
  if (content.startsWith('https://') || content.startsWith('http://') || content.startsWith('data:')) return content
  return ''
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    accounts.value = await listWeChatAccounts()
  } catch (value) {
    showError(value)
  } finally {
    loading.value = false
  }
}

function stopPolling(): void {
  if (timer) window.clearInterval(timer)
  timer = 0
}

async function refreshBind(): Promise<void> {
  if (!session.value || session.value.status === 'confirmed') return
  try {
    const next = await pollWeChatBind(session.value.session_id, verifyCode.value.trim())
    session.value = next
    if (next.status === 'confirmed') {
      stopPolling()
      verifyCode.value = ''
      await load()
    }
  } catch (value) {
    showError(value)
  }
}

async function beginBind(): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  stopPolling()
  try {
    session.value = await startWeChatBind()
    await refreshBind()
    if (session.value?.status !== 'confirmed') timer = window.setInterval(() => void refreshBind(), 2000)
  } catch (value) {
    showError(value)
  } finally {
    busy.value = false
  }
}

async function unbind(accountId: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await unbindWeChat(accountId)
    await load()
  } catch (value) {
    showError(value)
  } finally {
    busy.value = false
  }
}

onMounted(() => void load())
onUnmounted(stopPolling)
</script>

<template>
  <section class="wechat">
    <header class="wechat__header">
      <h1>微信</h1>
      <p>用微信和你的 Agent 一对一对话。扫码后，这个微信号只绑定当前登录账号。</p>
    </header>
    <p
      v-if="error"
      class="wechat__error"
      role="alert"
    >
      {{ error }}
    </p>
    <p v-if="loading">
      正在读取绑定状态…
    </p>
    <ul
      v-else
      class="wechat__list"
    >
      <li v-if="!accounts.length">
        还没有绑定微信。
      </li>
      <li
        v-for="item in accounts"
        :key="item.id"
        class="wechat__account"
      >
        <div>
          <strong>{{ statusText[item.status] ?? item.status }}</strong>
          <span>微信用户 {{ item.ilink_user_id || '未知' }}</span>
          <span>{{ item.window_note }}</span>
        </div>
        <button
          type="button"
          :disabled="busy"
          @click="unbind(item.id)"
        >
          解除绑定
        </button>
        <ul
          v-if="item.skipped_pushes.length"
          class="wechat__skipped"
        >
          <li
            v-for="push in item.skipped_pushes"
            :key="push.created_at + push.excerpt"
          >
            未推送（{{ reasonText(push.reason) }}）：{{ push.excerpt }}
          </li>
        </ul>
      </li>
    </ul>
    <button
      type="button"
      class="wechat__bind"
      :disabled="busy"
      @click="beginBind"
    >
      {{ accounts.length ? '重新绑定' : '绑定微信' }}
    </button>
    <div
      v-if="session && session.status !== 'confirmed'"
      class="wechat__qr"
    >
      <p>{{ session.message || statusText[session.status] || '等待扫码' }}</p>
      <img
        v-if="imageUrl(session.qrcode_img_content)"
        :src="imageUrl(session.qrcode_img_content)"
        alt="微信绑定二维码"
      />
      <p v-else>
        {{ session.qrcode_img_content }}
      </p>
      <label v-if="session.status.includes('need_verifycode')">
        手机上的数字
        <input
          v-model="verifyCode"
          inputmode="numeric"
          maxlength="8"
          @change="refreshBind"
        />
      </label>
    </div>
    <aside class="wechat__help">
      <p>绑定后在微信里直接发消息。指令：/new 新对话，/ws 空间标识 切换空间，/agent 切换 Agent。</p>
      <p>写操作会发来确认码，回复「同意 确认码」或「拒绝 确认码 原因」。超过 24 小时没有发消息时，定时结果只留在网页。</p>
    </aside>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.wechat { max-width: 720px; display: grid; gap: 16px; }
.wechat__header h1 { margin: 0 0 8px; font-size: 28px; }
.wechat__header p, .wechat__help { color: $muted; margin: 0; }
.wechat__error { color: #9d2c2c; margin: 0; }
.wechat__list { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
.wechat__account { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 12px; border: 1px solid $line; border-radius: 8px; background: #fff; }
.wechat__account span { display: block; color: $muted; font-size: 13px; }
.wechat__account { flex-wrap: wrap; }
.wechat__skipped { width: 100%; margin: 8px 0 0; padding-left: 18px; color: $muted; font-size: 13px; }
.wechat__bind, .wechat__account button { border: 0; background: $green; color: #fff; border-radius: 6px; padding: 8px 14px; }
.wechat__qr { display: grid; gap: 8px; padding: 16px; border: 1px dashed $line; border-radius: 8px; background: $paper; }
.wechat__qr img { width: 220px; height: 220px; object-fit: contain; background: #fff; }
.wechat__help { display: grid; gap: 6px; font-size: 13px; }
</style>
