<script setup lang="ts">
import { toDataURL } from 'qrcode'
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
const qrImageUrl = ref('')
const qrRenderError = ref('')
const verifyCode = ref('')
const error = ref('')
const loading = ref(false)
const busy = ref(false)
let timer = 0
let pollRun = 0
let qrRenderRun = 0

const POLL_INTERVAL_MS = 2000
const QR_WIDTH_PX = 220

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

function externalQrUrl(content: string): string {
  if (content.startsWith('https://') || content.startsWith('http://')) return content
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
  if (timer) window.clearTimeout(timer)
  timer = 0
  // 让已在飞行的旧请求失效，避免重新绑定后被旧响应覆盖。
  pollRun += 1
}

async function updateSession(next: WeChatBindSession): Promise<void> {
  const previousContent = session.value?.qrcode_img_content ?? ''
  session.value = next
  if (next.qrcode_img_content === previousContent) return

  const renderRun = ++qrRenderRun
  qrImageUrl.value = ''
  qrRenderError.value = ''
  if (!next.qrcode_img_content) return

  try {
    const image = await toDataURL(next.qrcode_img_content, {
      errorCorrectionLevel: 'M',
      margin: 1,
      width: QR_WIDTH_PX,
    })
    if (renderRun === qrRenderRun) qrImageUrl.value = image
  } catch {
    if (renderRun === qrRenderRun) qrRenderError.value = '二维码生成失败，请使用备用链接'
  }
}

async function refreshBind(run: number): Promise<void> {
  const current = session.value
  if (run !== pollRun || !current || current.status === 'confirmed') return
  try {
    const next = await pollWeChatBind(current.session_id, verifyCode.value.trim())
    if (run !== pollRun) return
    error.value = ''
    await updateSession(next)
    if (next.status === 'confirmed') {
      stopPolling()
      verifyCode.value = ''
      await load()
    }
  } catch (value) {
    if (run === pollRun) showError(value)
  } finally {
    // 上一次请求结束后再计时，避免慢请求叠加压垮后端。
    if (run === pollRun && session.value?.status !== 'confirmed') {
      timer = window.setTimeout(() => void refreshBind(run), POLL_INTERVAL_MS)
    }
  }
}

function startPolling(): void {
  stopPolling()
  const run = pollRun
  void refreshBind(run)
}

async function beginBind(): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  stopPolling()
  try {
    await updateSession(await startWeChatBind())
    startPolling()
  } catch (value) {
    showError(value)
  } finally {
    busy.value = false
  }
}

function submitVerifyCode(): void {
  startPolling()
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
        v-if="qrImageUrl"
        :src="qrImageUrl"
        alt="微信绑定二维码"
      />
      <p v-else-if="session.qrcode_img_content && !qrRenderError">
        正在生成二维码…
      </p>
      <p
        v-if="qrRenderError"
        class="wechat__qr-error"
      >
        {{ qrRenderError }}
      </p>
      <a
        v-if="externalQrUrl(session.qrcode_img_content)"
        :href="externalQrUrl(session.qrcode_img_content)"
        target="_blank"
        rel="noopener noreferrer"
      >
        二维码无法扫描时，在浏览器打开
      </a>
      <label v-if="session.status.includes('need_verifycode')">
        手机上的数字
        <input
          v-model="verifyCode"
          inputmode="numeric"
          maxlength="8"
          @change="submitVerifyCode"
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

.wechat {
  display: grid;
  gap: 14px;
  max-width: 1180px;
  margin: 0 auto;
  padding: 34px clamp(16px, 4vw, 52px) 48px;

  &__header { margin-bottom: 8px; }
  &__header h1 { @include page-title; margin-bottom: 7px; }
  &__header p, &__help { color: $muted; font-size: 13px; }
  &__error { padding: 9px 11px; border-radius: $control-radius; color: $log-error; background: $log-error-bg; }
  &__list, &__qr {
    border: 1px solid $line;
    border-radius: $panel-radius;
    background: $surface;
  }
  &__list { list-style: none; padding: 0; margin: 0; }
  &__list > li { padding: 15px 16px; }
  &__list > li + li { border-top: 1px solid $line; }
  &__list > li:not(.wechat__account) { color: $muted; font-size: 13px; }
  &__account { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; align-items: center; }
  &__account strong { color: $green; }
  &__account span { display: block; color: $muted; font-size: 13px; overflow-wrap: anywhere; }
  &__skipped {
    width: 100%; margin: 0; padding: 9px 12px 9px 26px; border-radius: $control-radius;
    background: $log-warning-bg; color: $log-warning; font-size: 13px; overflow-wrap: anywhere;
  }
  &__bind, &__account button { min-height: 36px; border: 1px solid $line; border-radius: $control-radius; padding: 7px 12px; }
  &__bind {
    justify-self: start; border-color: $green; background: $green; color: $on-green; font-weight: 650;
    &:enabled:hover { background: $green-hover; }
  }
  &__account button { background: $surface; color: $log-error; &:enabled:hover { border-color: $log-error; } }
  &__qr {
    display: grid; gap: 8px; padding: 16px;
    img { width: min(220px, 100%); height: auto; aspect-ratio: 1; object-fit: contain; background: #fff; }
    a { width: fit-content; }
    label { display: grid; gap: 6px; max-width: 15rem; }
    input { min-width: 0; padding: 8px 10px; border: 1px solid $line; border-radius: $control-radius; background: $paper; }
  }
  &__qr-error { color: $log-error; }
  &__help { display: grid; gap: 6px; padding: 12px 14px; border-radius: $panel-radius; background: $sidebar; }
}
</style>
