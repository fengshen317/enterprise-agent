import { ApiError, requestJson } from './client'

const WECHAT_BIND_TIMEOUT_MS = 20000
const WECHAT_POLL_TIMEOUT_MS = 15000

export interface WeChatBindSession {
  session_id: string
  status: string
  qrcode_img_content: string
  account_id: string | null
  ilink_user_id: string
  message: string
}

export interface SkippedPush {
  created_at: string
  reason: string
  excerpt: string
}

export interface WeChatAccount {
  id: string
  workspace_id: string
  status: string
  ilink_bot_id: string
  ilink_user_id: string
  paused_until: string | null
  created_at: string
  window_open: boolean
  window_until: string | null
  window_note: string
  skipped_pushes: SkippedPush[]
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function invalid(): never {
  throw new ApiError('微信渠道接口返回格式不正确', 502)
}

function parseBind(value: unknown): WeChatBindSession {
  if (
    !record(value)
    || typeof value.session_id !== 'string'
    || typeof value.status !== 'string'
    || typeof value.qrcode_img_content !== 'string'
    || (value.account_id !== null && typeof value.account_id !== 'string')
    || typeof value.ilink_user_id !== 'string'
    || typeof value.message !== 'string'
  ) return invalid()
  return value as unknown as WeChatBindSession
}

function parseAccount(value: unknown): WeChatAccount {
  if (
    !record(value)
    || typeof value.id !== 'string'
    || typeof value.workspace_id !== 'string'
    || typeof value.status !== 'string'
    || typeof value.ilink_bot_id !== 'string'
    || typeof value.ilink_user_id !== 'string'
    || (value.paused_until !== null && typeof value.paused_until !== 'string')
    || typeof value.created_at !== 'string'
    || typeof value.window_open !== 'boolean'
    || (value.window_until !== null && typeof value.window_until !== 'string')
    || typeof value.window_note !== 'string'
    || !Array.isArray(value.skipped_pushes)
  ) return invalid()
  return value as unknown as WeChatAccount
}

export async function startWeChatBind(): Promise<WeChatBindSession> {
  // iLink 取码请求最长等待 15 秒，前端必须留出服务端处理余量。
  return parseBind(await requestJson('/api/channels/wechat/bind', { method: 'POST' }, WECHAT_BIND_TIMEOUT_MS))
}

export async function pollWeChatBind(sessionId: string, verifyCode = ''): Promise<WeChatBindSession> {
  const query = verifyCode ? `?verify_code=${encodeURIComponent(verifyCode)}` : ''
  // 后端连接 iLink 时可以等待 10 秒，避免浏览器先行超时而误报服务不可用。
  return parseBind(await requestJson(`/api/channels/wechat/bind/${sessionId}${query}`, {}, WECHAT_POLL_TIMEOUT_MS))
}

export async function listWeChatAccounts(): Promise<WeChatAccount[]> {
  const body = await requestJson('/api/channels/wechat')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseAccount)
}

export async function unbindWeChat(accountId: string): Promise<void> {
  await requestJson(`/api/channels/wechat/${accountId}/unbind`, { method: 'POST' })
}

export type EnterpriseChannel = 'feishu' | 'dingtalk' | 'qq'

export interface EnterpriseApp {
  channel_type: EnterpriseChannel
  external_app_id: string
  status: string
  secret_configured: boolean
  robot_code: string
  sandbox: boolean
  updated_at: string | null
}

export interface EnterpriseAppInput {
  channel_type: EnterpriseChannel
  external_app_id: string
  secret: string
  robot_code: string
  sandbox: boolean
}

export interface BindCode {
  code: string
  expires_at: string
  instructions: string
}

export interface EnterpriseBinding {
  bound: boolean
  account_id: string | null
  platform_user_id: string
  status: string
  app_configured: boolean
}

function parseApp(value: unknown): EnterpriseApp {
  if (
    !record(value)
    || (value.channel_type !== 'feishu' && value.channel_type !== 'dingtalk' && value.channel_type !== 'qq')
    || typeof value.external_app_id !== 'string'
    || typeof value.status !== 'string'
    || typeof value.secret_configured !== 'boolean'
    || typeof value.robot_code !== 'string'
    || typeof value.sandbox !== 'boolean'
    || (value.updated_at !== null && value.updated_at !== undefined && typeof value.updated_at !== 'string')
  ) return invalid()
  return {
    channel_type: value.channel_type,
    external_app_id: value.external_app_id,
    status: value.status,
    secret_configured: value.secret_configured,
    robot_code: value.robot_code,
    sandbox: value.sandbox,
    updated_at: typeof value.updated_at === 'string' ? value.updated_at : null,
  }
}

export async function listEnterpriseApps(): Promise<EnterpriseApp[]> {
  const body = await requestJson('/api/channels/apps')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseApp)
}

export async function saveEnterpriseApp(input: EnterpriseAppInput): Promise<EnterpriseApp> {
  return parseApp(await requestJson('/api/channels/apps', { method: 'POST', body: JSON.stringify(input) }))
}

export async function loadEnterpriseBinding(channel: EnterpriseChannel): Promise<EnterpriseBinding> {
  const value = await requestJson(`/api/channels/${channel}/binding`)
  if (
    !record(value)
    || typeof value.bound !== 'boolean'
    || (value.account_id !== null && typeof value.account_id !== 'string')
    || typeof value.platform_user_id !== 'string'
    || typeof value.status !== 'string'
    || typeof value.app_configured !== 'boolean'
  ) return invalid()
  return value as unknown as EnterpriseBinding
}

export async function createBindCode(channel: EnterpriseChannel): Promise<BindCode> {
  const value = await requestJson(`/api/channels/${channel}/bind-code`, { method: 'POST' })
  if (!record(value) || typeof value.code !== 'string' || typeof value.expires_at !== 'string' || typeof value.instructions !== 'string') {
    return invalid()
  }
  return value as unknown as BindCode
}

export async function unbindEnterprise(channel: EnterpriseChannel): Promise<void> {
  await requestJson(`/api/channels/${channel}/unbind`, { method: 'POST' })
}
