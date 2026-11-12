import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as api from '../api/auth'
import { ApiError, setSecurityContext } from '../api/client'

export const useAuthStore = defineStore('auth', () => {
  const session = ref<api.Session | null>(null)
  const selectedWorkspaceId = ref('')
  const initialized = ref(false)
  const authenticated = computed(() => session.value !== null)
  const activeWorkspace = computed(() => session.value?.workspaces.find(item => item.id === selectedWorkspaceId.value))
  let epoch = 0
  let restoring: Promise<boolean> | null = null
  function clear(): void {
    epoch++
    session.value = null; selectedWorkspaceId.value = ''; initialized.value = true
    setSecurityContext('', '')
  }
  function apply(value: api.Session): void {
    session.value = value
    const remembered = localStorage.getItem('ea-workspace:' + value.user.id)
    selectedWorkspaceId.value = value.workspaces.find(item => item.id === remembered)?.id ?? value.workspaces[0]?.id ?? ''
    setSecurityContext(value.csrf_token, selectedWorkspaceId.value)
    initialized.value = true
  }
  async function restore(): Promise<boolean> {
    if (restoring) return restoring
    const version = epoch
    restoring = (async () => {
      try {
        const value = await api.getSession()
        if (epoch !== version) return false
        apply(value)
        return true
      } catch (error) {
        if (epoch === version && error instanceof ApiError && error.status === 401) { clear(); return false }
        throw error
      } finally { restoring = null }
    })()
    return restoring
  }
  async function signIn(username: string, password: string): Promise<void> {
    const version = ++epoch
    const value = await api.login(username, password)
    if (version !== epoch) return
    apply(value)
    localStorage.setItem('ea-auth-event', crypto.randomUUID())
  }
  async function signOut(): Promise<void> {
    await api.logout()
    clear()
    localStorage.setItem('ea-auth-event', crypto.randomUUID())
  }
  function selectWorkspace(id: string): void {
    if (!session.value?.workspaces.some(item => item.id === id)) throw new Error('不可访问该工作空间')
    selectedWorkspaceId.value = id
    setSecurityContext(session.value.csrf_token, id)
    localStorage.setItem('ea-workspace:' + session.value.user.id, id)
  }
  return { session, selectedWorkspaceId, initialized, authenticated, activeWorkspace, restore, signIn, signOut, selectWorkspace, clear }
})
