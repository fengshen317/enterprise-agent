import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { listConnectors, type ConnectorView } from '../api/connectors'
import { useAuthStore } from './auth'

const REFRESH_INTERVAL_MS = 15_000

export const useConnectorsStore = defineStore('connectors', () => {
  const auth = useAuthStore()
  const connectors = ref<ConnectorView[]>([])
  const error = ref('')
  const loading = ref(false)
  const scope = computed(() => auth.session && auth.selectedWorkspaceId
    ? `${auth.session.user.id}:${auth.selectedWorkspaceId}` : '')
  const attention = computed(() => connectors.value.filter(item => item.enabled
    && (item.auth_action === 'authorize' || item.auth_action === 'configure' || item.auth_status === 'required')))
  let generation = 0
  let consumers = 0
  let pending: Promise<void> | null = null
  let timer: ReturnType<typeof setTimeout> | undefined

  function clearTimer(): void {
    clearTimeout(timer)
    timer = undefined
  }

  function schedule(): void {
    clearTimer()
    if (consumers && scope.value && document.visibilityState !== 'hidden') {
      timer = setTimeout(() => { void refresh() }, REFRESH_INTERVAL_MS)
    }
  }

  async function refresh(force = false): Promise<void> {
    const version = generation
    if (pending) {
      await pending
      if (!force || version !== generation) return
      return refresh()
    }
    if (!scope.value) return
    clearTimer()
    loading.value = connectors.value.length === 0
    const request = (async () => {
      try {
        const result = await listConnectors()
        if (version === generation) { connectors.value = result; error.value = '' }
      } catch (value) {
        if (version === generation) error.value = value instanceof Error ? value.message : '连接器状态刷新失败'
      } finally {
        if (version === generation) { loading.value = false; pending = null; schedule() }
      }
    })()
    pending = request
    await request
  }

  function visibilityChanged(): void {
    clearTimer()
    if (document.visibilityState !== 'hidden') void refresh()
  }

  function acquire(): void {
    consumers++
    if (consumers === 1) {
      document.addEventListener('visibilitychange', visibilityChanged)
      void refresh()
    }
  }

  function release(): void {
    consumers = Math.max(0, consumers - 1)
    if (!consumers) {
      clearTimer()
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }

  watch(scope, () => {
    // 空间与用户变化立即隔离迟到响应，避免展示上一空间的授权状态。
    generation++
    clearTimer()
    pending = null
    connectors.value = []
    error.value = ''
    loading.value = false
    if (consumers) void refresh()
  }, { flush: 'sync' })

  return { connectors, attention, error, loading, scope, refresh, acquire, release }
})
