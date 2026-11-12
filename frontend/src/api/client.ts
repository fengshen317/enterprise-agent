const REQUEST_TIMEOUT_MS = 5000
const HTTP_TIMEOUT_STATUS = 408
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

let currentCsrf = ''
let currentWorkspace = ''
export function setSecurityContext(csrf: string, workspace: string): void { currentCsrf = csrf; currentWorkspace = workspace }
export function getWorkspaceId(): string { return currentWorkspace }
export function notifyUnauthorized(): void { window.dispatchEvent(new Event('auth-expired')) }

export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly requestId: string | null = null) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function requestJson(path: string, init: RequestInit = {}, timeoutMs = REQUEST_TIMEOUT_MS): Promise<unknown> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(API_BASE_URL + path, { ...init, credentials: 'include', signal: controller.signal, headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...(currentCsrf ? { 'X-CSRF-Token': currentCsrf } : {}), ...(currentWorkspace ? { 'X-Workspace-ID': currentWorkspace } : {}), ...init.headers } })
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/api/auth/')) notifyUnauthorized()
      // 只展示后端定义的公开错误，不直接回显代理页或服务器异常文本。
      let message = '服务暂时不可用'
      try {
        const body: unknown = await response.json()
        if (typeof body === 'object' && body !== null && 'error' in body) {
          const error = body.error
          if (typeof error === 'object' && error !== null && 'message' in error && typeof error.message === 'string') message = error.message
        }
      } catch { /* 非 JSON 错误使用统一提示。 */ }
      throw new ApiError(message, response.status, response.headers.get('X-Request-ID'))
    }
    if (response.status === 204) return null
    return await response.json() as unknown
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (controller.signal.aborted) throw new ApiError('连接超时，请检查后端服务', HTTP_TIMEOUT_STATUS)
    throw new ApiError('无法连接服务，请稍后重试', 0)
  } finally {
    clearTimeout(timeout)
  }
}

export async function getJson(path: string): Promise<unknown> {
  return requestJson(path)
}

const FORM_TIMEOUT_MS = 60000

export async function requestForm(path: string, form: FormData): Promise<unknown> {
  // 上传不能复用 JSON 请求：浏览器需要自行设置 multipart boundary。
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), FORM_TIMEOUT_MS)
  try {
    const response = await fetch(API_BASE_URL + path, {
      method: 'POST',
      credentials: 'include',
      body: form,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(currentCsrf ? { 'X-CSRF-Token': currentCsrf } : {}),
        ...(currentWorkspace ? { 'X-Workspace-ID': currentWorkspace } : {}),
      },
    })
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/api/auth/')) notifyUnauthorized()
      let message = '服务暂时不可用'
      try {
        const body: unknown = await response.json()
        if (typeof body === 'object' && body !== null && 'error' in body) {
          const error = body.error
          if (typeof error === 'object' && error !== null && 'message' in error && typeof error.message === 'string') message = error.message
        }
      } catch { /* 非 JSON 错误使用统一提示。 */ }
      throw new ApiError(message, response.status, response.headers.get('X-Request-ID'))
    }
    if (response.status === 204) return null
    return await response.json() as unknown
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (controller.signal.aborted) throw new ApiError('上传超时，请检查后端服务', HTTP_TIMEOUT_STATUS)
    throw new ApiError('无法连接服务，请稍后重试', 0)
  } finally {
    clearTimeout(timeout)
  }
}
