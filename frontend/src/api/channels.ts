import { ApiError, requestJson } from './client'

export interface WeChatBindSession {
  session_id: string
  status: string
  qrcode_img_content: string
  account_id: string | null
  ilink_user_id: string
  message: string
}

export interface WeChatAccount {
  id: string
  workspace_id: string
  status: string
  ilink_bot_id: string
  ilink_user_id: string
  paused_until: string | null
  created_at: string
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
  ) return invalid()
  return value as unknown as WeChatAccount
}

export async function startWeChatBind(): Promise<WeChatBindSession> {
  return parseBind(await requestJson('/api/channels/wechat/bind', { method: 'POST' }))
}

export async function pollWeChatBind(sessionId: string, verifyCode = ''): Promise<WeChatBindSession> {
  const query = verifyCode ? `?verify_code=${encodeURIComponent(verifyCode)}` : ''
  return parseBind(await requestJson(`/api/channels/wechat/bind/${sessionId}${query}`, {}, 8000))
}

export async function listWeChatAccounts(): Promise<WeChatAccount[]> {
  const body = await requestJson('/api/channels/wechat')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseAccount)
}

export async function unbindWeChat(accountId: string): Promise<void> {
  await requestJson(`/api/channels/wechat/${accountId}/unbind`, { method: 'POST' })
}
