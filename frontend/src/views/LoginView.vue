<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ApiError } from '../api/client'

const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
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
      <span class="login__brand">≋ 序列</span><div>
        <p class="login__eyebrow">
          SEQUENCE / AGENT WORKSPACE
        </p><h1>让想法有回应。<br />让工作有进展。</h1><p class="login__description">
          在属于你的工作空间里，<br />连接问题、知识与下一步行动。
        </p>
      </div><span class="login__edition">本地工作台 · 从一个问题，到完整交付</span>
    </section>
    <section class="login__panel">
      <form
        class="login__form"
        @submit.prevent="signIn"
      >
        <p class="login__eyebrow">
          欢迎回来
        </p><h2>登录工作台</h2><p class="login__hint">
          使用管理员为你创建的账号继续。
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
.login { min-height: 100dvh; display: grid; grid-template-columns: 48% 1fr;
  &__story { background: #173b32; color: #e5eedb; padding: 55px 12%; display: flex; flex-direction: column; justify-content: space-between; min-height: 700px; }
  &__brand { font-size: 30px; letter-spacing: 5px; }
  &__eyebrow { font-size: 10px; letter-spacing: 2px; color: #91a983; }
  h1 { font-weight: 400; font-size: clamp(30px, 3.6vw, 52px); line-height: 1.65; margin: 30px 0; letter-spacing: -1px; }
  &__description { font-size: 13px; color: #b4c9ad; line-height: 2; }
  &__edition { color: #9eb697; font-size: 10px; letter-spacing: 1px; margin-top: 50px; }
  &__panel { display: grid; place-items: center; padding: 45px 30px; }
  &__form { width: min(100%, 360px); h2 { font-size: 30px; font-weight: 400; margin: 18px 0 12px; } label { display: block; margin: 24px 0 10px; font-size: 12px; } input { width: 100%; font: inherit; background: white; border: 1px solid $line; border-radius: 6px; padding: 13px; color: $ink; &:focus { outline: 2px solid #92ae7a; outline-offset: 2px; } } button { margin-top: 28px; width: 100%; border: 0; background: #31533d; color: #eff5e5; border-radius: 6px; padding: 14px; } }
  &__hint, &__footer { color: $muted; font-size: 12px; line-height: 1.9; }
  &__footer { margin-top: 22px; font-size: 11px; }
  &__error { color: #934b40; background: #faeee6; border-radius: 5px; padding: 10px; font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; margin-top: 15px; }
}
@media (max-width: 760px) { .login { grid-template-columns: 1fr; &__story { min-height: auto; padding: 25px; h1, .login__description, .login__edition { display: none; } } &__panel { padding: 45px 24px; } } }
</style>
