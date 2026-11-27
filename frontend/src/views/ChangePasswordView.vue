<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'

const MIN_PASSWORD_LENGTH = 8
const MAX_PASSWORD_LENGTH = 128
const auth = useAuthStore()
const router = useRouter()
const currentPassword = ref('')
const newPassword = ref('')
const confirmation = ref('')
const busy = ref(false)
const error = ref('')

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = ''
  const length = Array.from(newPassword.value).length
  if (!newPassword.value.trim() || length < MIN_PASSWORD_LENGTH || length > MAX_PASSWORD_LENGTH) {
    error.value = `新密码需为 ${MIN_PASSWORD_LENGTH} 至 ${MAX_PASSWORD_LENGTH} 个字符，且不能全为空白。`
    return
  }
  if (newPassword.value !== confirmation.value) {
    error.value = '两次输入的新密码不一致。'
    return
  }
  busy.value = true
  try {
    await auth.changePassword(currentPassword.value, newPassword.value)
    await router.replace({ name: 'login', query: { password_changed: '1' } })
  } catch (value) {
    if (value instanceof ApiError && value.status === 401) await router.replace({ name: 'login' })
    else error.value = value instanceof Error ? value.message : '修改密码失败，请重试。'
  } finally {
    currentPassword.value = ''
    newPassword.value = ''
    confirmation.value = ''
    busy.value = false
  }
}

async function signOut(): Promise<void> {
  busy.value = true
  try {
    await auth.signOut()
    await router.replace({ name: 'login' })
  } catch (value) {
    error.value = value instanceof Error ? value.message : '退出失败，请重试。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section
    class="password-page"
    :class="{ 'password-page--required': auth.session?.must_change_password }"
    aria-labelledby="password-title"
  >
    <form
      class="password-page__form"
      @submit.prevent="submit"
    >
      <h1 id="password-title">
        {{ auth.session?.must_change_password ? '修改初始密码' : '修改密码' }}
      </h1>
      <p class="password-page__hint">
        <template v-if="auth.session?.must_change_password">
          请为账号 <strong>{{ auth.session.user.username }}</strong> 设置个人密码。保存后请使用新密码登录。
        </template>
        <template v-else>
          修改密码后，已有会话将退出。请使用新密码重新登录。
        </template>
      </p>
      <fieldset :disabled="busy">
        <!-- 旧账号可能使用空密码，当前密码是否正确由服务端核验。 -->
        <label for="current-password">{{ auth.session?.must_change_password ? '初始密码' : '当前密码' }}</label>
        <input
          id="current-password"
          v-model="currentPassword"
          type="password"
          autocomplete="current-password"
        />
        <label for="new-password">新密码</label>
        <input
          id="new-password"
          v-model="newPassword"
          type="password"
          autocomplete="new-password"
          aria-describedby="new-password-hint"
          required
        />
        <p
          id="new-password-hint"
          class="password-page__hint"
        >
          {{ MIN_PASSWORD_LENGTH }}–{{ MAX_PASSWORD_LENGTH }} 个字符
        </p>
        <label for="confirm-password">确认新密码</label>
        <input
          id="confirm-password"
          v-model="confirmation"
          type="password"
          autocomplete="new-password"
          required
        />
      </fieldset>
      <p
        v-if="error"
        class="password-page__error"
        role="alert"
      >
        {{ error }}
      </p>
      <div class="password-page__actions">
        <button
          class="password-page__save"
          type="submit"
          :disabled="busy"
        >
          {{ busy ? '正在处理…' : '保存新密码' }}
        </button>
        <button
          v-if="auth.session?.must_change_password"
          class="password-page__exit"
          type="button"
          :disabled="busy"
          @click="signOut"
        >
          退出登录
        </button>
      </div>
    </form>
  </section>
</template>

<style scoped lang="scss">
@use '../styles/variables' as *;

.password-page {
  padding: 40px clamp(16px, 4vw, 56px);
  max-width: 1200px;
  margin-inline: auto;

  &--required {
    display: grid;
    place-items: center;
    min-height: 100dvh;
    max-width: none;
    background: $hero-glow;
  }

  &__form {
    width: min(100%, 460px);
    padding: clamp(20px, 5vw, 32px);
    border: 1px solid $line;
    border-radius: $panel-radius;
    background: $surface;

    h1 { @include page-title; margin-bottom: 10px; }
    fieldset { margin: 0; padding: 0; border: 0; min-width: 0; }
    label { display: block; margin: 16px 0 6px; font-size: 13px; font-weight: 600; }
    input {
      width: 100%; min-height: 42px; background: $paper;
      border: 1px solid $line; border-radius: $control-radius; padding: 8px 11px;
    }
  }

  &__hint { margin-top: 5px; color: $muted; font-size: 13px; line-height: 1.65; }
  &__error { margin-top: 16px; padding: 10px; border-radius: $control-radius; background: $log-error-bg; color: $log-error; }
  &__actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
  &__save, &__exit { min-height: 40px; padding: 8px 14px; border-radius: $control-radius; font-weight: 600; }
  &__save { border: 1px solid $green; background: $green; color: $on-green; &:enabled:hover { background: $green-hover; } }
  &__exit { border: 1px solid $line; background: $paper; color: $green; }
}
</style>
