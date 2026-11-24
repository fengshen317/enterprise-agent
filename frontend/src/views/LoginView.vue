<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ApiError } from '../api/client'
import { createInitialAdmin, getInitialSetup, type InitialSetupStatus } from '../api/initialSetup'

const MIN_PASSWORD_LENGTH = 4
const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
const setup = ref<InitialSetupStatus | null>(null)
const setupToken = ref('')
const setupUsername = ref('admin')
const displayName = ref('管理员')
const setupPassword = ref('')
const confirmPassword = ref('')
const setupBusy = ref(false)
const setupError = ref('')
const notice = ref('')

async function loadSetup(): Promise<void> {
  try {
    const current = await getInitialSetup()
    if (current.available) {
      setupUsername.value = current.username ?? 'admin'
      setup.value = current
    }
  } catch { /* 现有部署仍可照常提交登录。 */ }
}

async function submitSetup(): Promise<void> {
  if (setupBusy.value) return
  setupError.value = ''
  if (Array.from(setupPassword.value).length < MIN_PASSWORD_LENGTH) {
    setupError.value = `密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符。`
    return
  }
  if (setupPassword.value !== confirmPassword.value) {
    setupError.value = '两次输入的密码不一致。'
    return
  }
  setupBusy.value = true
  try {
    username.value = await createInitialAdmin({
      token: setupToken.value.trim(),
      username: setupUsername.value,
      display_name: displayName.value.trim(),
      password: setupPassword.value,
    })
    notice.value = '管理员已创建。请登录工作台。'
    error.value = ''
    setup.value = null
  } catch (value) {
    if (value instanceof ApiError && value.status === 409) {
      notice.value = '首次部署已结束。请使用已有管理员账号登录。'
      error.value = ''
      setup.value = null
    } else {
      setupError.value = value instanceof Error ? value.message : '创建管理员失败，请重试'
    }
  } finally {
    setupToken.value = ''
    setupPassword.value = ''
    confirmPassword.value = ''
    setupBusy.value = false
  }
}

onMounted(loadSetup)

async function signIn(): Promise<void> {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await auth.signIn(username.value, password.value); password.value = ''; await router.replace('/chat') }
  catch (value) {
    error.value = value instanceof Error ? value.message : '登录失败，请稍后重试'
    if (value instanceof ApiError && value.requestId) error.value += ' · 请求标识：' + value.requestId
    password.value = ''
  } finally { busy.value = false }
}
</script>

<template>
  <main class="login">
    <section class="login__story">
      <div class="login__brand">
        <span aria-hidden="true">≋</span>序列
      </div>
      <div>
        <p class="login__story-title">
          企业 Agent 工作台
        </p>
        <p class="login__description">
          在工作空间里提问、查找知识，并交给智能体完成任务。
        </p>
      </div>
    </section>
    <section class="login__panel">
      <form
        v-if="setup"
        class="login__form login__form--setup"
        @submit.prevent="submitSetup"
      >
        <p class="login__setup-label">
          首次部署
        </p>
        <h1>创建工作台管理员</h1>
        <p class="login__hint">
          验证部署口令后，创建管理员和默认工作空间。
        </p>
        <fieldset :disabled="setupBusy">
          <label for="setup-token">部署口令</label>
          <input
            id="setup-token"
            v-model="setupToken"
            type="password"
            autocomplete="off"
            minlength="32"
            maxlength="128"
            required
          />
          <details class="login__setup-help">
            <summary>在哪里查看部署口令</summary>
            <p>可使用服务器环境配置中的口令。使用自动生成的口令时，在部署目录运行以下命令。</p>
            <code>docker compose exec api cat /home/enterprise-agent/.enterprise-agent/setup-token</code>
          </details>
          <div class="login__setup-fields">
            <div>
              <label for="setup-username">管理员账号</label>
              <input
                id="setup-username"
                v-model.trim="setupUsername"
                :readonly="setup.username !== null"
                autocomplete="username"
                minlength="3"
                maxlength="64"
                required
              />
            </div>
            <div>
              <label for="setup-display-name">显示名称</label>
              <input
                id="setup-display-name"
                v-model.trim="displayName"
                autocomplete="name"
                maxlength="100"
                required
              />
            </div>
            <div>
              <label for="setup-password">设置密码</label>
              <input
                id="setup-password"
                v-model="setupPassword"
                type="password"
                autocomplete="new-password"
                :placeholder="`至少 ${MIN_PASSWORD_LENGTH} 个字符`"
                :minlength="MIN_PASSWORD_LENGTH"
                required
              />
            </div>
            <div>
              <label for="setup-confirm-password">确认密码</label>
              <input
                id="setup-confirm-password"
                v-model="confirmPassword"
                type="password"
                autocomplete="new-password"
                :minlength="MIN_PASSWORD_LENGTH"
                required
              />
            </div>
          </div>
        </fieldset>
        <p
          v-if="setupError"
          class="login__error"
          role="alert"
        >
          {{ setupError }}
        </p>
        <button
          type="submit"
          :disabled="setupBusy"
        >
          {{ setupBusy ? '正在创建…' : '创建管理员 →' }}
        </button>
      </form>
      <form
        v-else
        class="login__form"
        @submit.prevent="signIn"
      >
        <h1>登录工作台</h1><p class="login__hint">
          使用管理员为你创建的账号继续。
        </p><p
          v-if="notice"
          class="login__notice"
          role="status"
        >
          {{ notice }}
        </p><label for="username">账号</label><input
          id="username"
          v-model="username"
          autocomplete="username"
          maxlength="64"
          required
          :disabled="busy"
          placeholder="输入登录账号"
        /><label for="password">密码</label><input
          id="password"
          v-model="password"
          type="password"
          autocomplete="current-password"
          :disabled="busy"
          placeholder="输入密码"
        /><p
          v-if="error"
          class="login__error"
          role="alert"
        >
          {{ error }}
        </p><button
          type="submit"
          :disabled="busy || !username.trim()"
        >
          {{ busy ? '正在登录…' : '登录 →' }}
        </button><p class="login__footer">
          暂不开放注册。没有账号或忘记密码，请联系管理员。
        </p>
      </form>
    </section>
  </main>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;
.login {
  min-height: 100dvh;
  display: grid;
  grid-template-columns: minmax(0, 43fr) minmax(0, 57fr);

  &__story {
    display: grid;
    grid-template-rows: auto 1fr;
    align-items: center;
    gap: 24px;
    padding: clamp(28px, 6vw, 88px);
    background: linear-gradient(135deg, $hero-glow, $hero 78%);
    border-inline-end: 1px solid $line;
  }
  &__brand {
    display: flex; align-items: center; gap: 9px; font-size: 18px; font-weight: 750;
    span { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 8px; background: $green; color: $on-green; }
  }
  &__story-title { @include page-title; }
  &__description { max-width: 24em; margin-top: 12px; color: $muted; font-size: 15px; line-height: 1.8; }
  &__setup-label { color: $green; font-size: 12px; margin-bottom: 5px; }
  &__panel { display: grid; place-items: center; padding: 36px clamp(18px, 5vw, 64px); }
  &__form {
    width: min(100%, 420px);
    padding: clamp(18px, 4vw, 32px);
    border: 1px solid $line;
    border-radius: $panel-radius;
    background: $surface;
    h1 { @include page-title; font-size: clamp(23px, 2.5vw, 32px); margin-bottom: 8px; }
    label { display: block; margin: 17px 0 6px; font-size: 13px; font-weight: 600; }
    input { width: 100%; min-height: 42px; background: $paper; border: 1px solid $line; border-radius: $control-radius; padding: 8px 11px; }
    button {
      margin-top: 21px; width: 100%; min-height: 42px; border: 0; background: $green; color: $on-green;
      border-radius: $control-radius; padding: 8px 12px; font-weight: 650;
      &:enabled:hover { background: $green-hover; }
    }
  }
  &__hint, &__footer { color: $muted; font-size: 13px; line-height: 1.65; }
  &__footer { margin-top: 13px; font-size: 12px; }
  &__error, &__notice { border-radius: $control-radius; padding: 9px 11px; font-size: 13px; margin-top: 13px; overflow-wrap: anywhere; }
  &__error { color: $log-error; background: $log-error-bg; }
  &__notice { color: $log-success; background: $log-success-bg; }
  &__form--setup { width: min(100%, 480px); fieldset { margin: 0; padding: 0; border: 0; min-width: 0; } }
  &__setup-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
  &__setup-help { color: $muted; margin-top: 10px; font-size: 12px; line-height: 1.7; summary { color: $green; cursor: pointer; } code { display: block; padding: 8px 10px; background: $paper; color: $ink; overflow-wrap: anywhere; } }
}
@media (max-width: 760px) {
  .login {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto 1fr;
    &__story { display: block; padding: 18px; border-inline-end: 0; border-bottom: 1px solid $line; }
    &__story > :last-child { display: none; }
    &__panel { place-items: start center; padding-block: 24px; }
  }
}
@media (max-width: 430px) { .login__setup-fields { grid-template-columns: 1fr; } }
</style>
