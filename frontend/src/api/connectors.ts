import { ApiError, requestJson } from './client'

// 健康检查允许后端执行至 60 秒，授权启动还需等待授权地址生成。
const CONNECTOR_TEST_TIMEOUT_MS = 65_000
const CONNECTOR_AUTH_TIMEOUT_MS = 90_000
const CONNECTOR_QR_POLL_TIMEOUT_MS = 12_000
const HOLDINGS_SYNC_TIMEOUT_MS = 30_000
const IMA_ACCOUNT_CHECK_TIMEOUT_MS = 20_000

export type ConnectorKind = 'cli' | 'mcp' | 'api'
export type ConnectorStatus = 'disabled' | 'connected' | 'degraded' | 'error'

export interface ConnectorHealth {
  auth_status: 'unknown' | 'authorized' | 'required' | 'not_required'
  last_error_code: string | null
  last_checked_at: string | null
  last_success_at: string | null
  auth_action: 'authorize' | 'configure' | 'retry' | null
}

export interface ConnectorView extends ConnectorHealth {
  key: string
  kind: ConnectorKind
  name: string
  description: string
  icon: string
  enabled: boolean
  status: ConnectorStatus
  tool_count: number
  last_error: string | null
}

export interface ConnectorToolView {
  name: string
  description: string
  input_schema: Record<string, unknown>
  risk: string
  approval: string
}

export interface ConfigField {
  key: string
  label: string
  field_type: 'text' | 'secret' | 'number' | 'boolean' | 'select' | 'json'
  required: boolean
  default: string
  placeholder: string
  is_secret: boolean
}

export interface RssSource {
  name: string
  category: string
  url: string
  description?: string
}

export async function listRssSources(): Promise<RssSource[]> {
  const body = await requestJson('/api/connectors/rss/sources')
  if (!Array.isArray(body) || !body.every(item => record(item)
    && typeof item.name === 'string' && typeof item.category === 'string'
    && typeof item.url === 'string'
    && (item.url.startsWith('https://') || (item.url === '' && typeof item.description === 'string' && item.description.trim().length > 0))
    && (item.description === undefined || typeof item.description === 'string'))) return invalid()
  return body as RssSource[]
}

export interface ConnectorDetail extends ConnectorHealth {
  key: string
  kind: ConnectorKind
  name: string
  description: string
  icon: string
  enabled: boolean
  status: ConnectorStatus
  last_error: string | null
  last_checked_at: string | null
  config_fields: ConfigField[]
  config_values: Record<string, string>
  tools: ConnectorToolView[]
  can_view_holdings?: boolean
  personal_account?: PersonalAccount | null
}

export interface PersonalAccount {
  bound: boolean
  auth_mode?: 'credentials' | 'scan'
  guide_url: string
  fields: { key: string; label: string; secret: boolean }[]
}

export interface ImaProbeResult {
  status: 'success' | 'error'
  error_code: string | null
  diagnostic_id: string | null
}

export interface ImaAccountCheckResult {
  knowledge_base: ImaProbeResult
  notes: ImaProbeResult
}

export function isPersonalConnector(key?: string): boolean {
  return ['qq_mail', 'ima', 'tencent_news', 'tavily_search', 'tmeet', 'yangjibao'].includes(key ?? '')
}

function parsePersonalAccount(value: unknown): PersonalAccount {
  if (!record(value) || typeof value.bound !== 'boolean' || typeof value.guide_url !== 'string'
    || !value.guide_url.startsWith('https://') || !Array.isArray(value.fields)
    || !value.fields.every(field => record(field) && typeof field.key === 'string'
      && typeof field.label === 'string' && typeof field.secret === 'boolean')) return invalid()
  return value as unknown as PersonalAccount
}

export async function savePersonalAccount(key: string, credentials: Record<string, string>): Promise<PersonalAccount> {
  return parsePersonalAccount(await requestJson('/api/connectors/' + encodeURIComponent(key) + '/my-account', {
    method: 'PUT', body: JSON.stringify({ credentials }),
  }))
}

export async function removePersonalAccount(key: string): Promise<PersonalAccount> {
  return parsePersonalAccount(await requestJson('/api/connectors/' + encodeURIComponent(key) + '/my-account', {
    method: 'DELETE',
  }))
}

function parseImaProbe(value: unknown): ImaProbeResult {
  if (!record(value) || (value.status !== 'success' && value.status !== 'error')
    || (value.status === 'success' ? value.error_code !== null : typeof value.error_code !== 'string')
    || (value.diagnostic_id !== null && typeof value.diagnostic_id !== 'string')) return invalid()
  return value as unknown as ImaProbeResult
}

export async function checkImaAccount(): Promise<ImaAccountCheckResult> {
  const body = await requestJson('/api/connectors/ima/my-account/check', {
    method: 'POST', cache: 'no-store',
  }, IMA_ACCOUNT_CHECK_TIMEOUT_MS)
  if (!record(body)) return invalid()
  return { knowledge_base: parseImaProbe(body.knowledge_base), notes: parseImaProbe(body.notes) }
}

export interface YangJiBaoHolding {
  code: string
  name: string
  holding_amount: number | null
  estimated_change_percent: number | null
  latest_nav_change_percent: number | null
  quote_time: string | null
  nav_date: string | null
  share?: number | null
  cost_amount?: number | null
  cost_nav?: number | null
  estimated_amount?: number | null
  latest_nav_amount?: number | null
  estimated_today_profit_amount?: number | null
}

export interface YangJiBaoPortfolio {
  synced_at: string
  fund_count: number
  funds: YangJiBaoHolding[]
}

export interface ConnectorTestResult extends ConnectorHealth {
  status: ConnectorStatus
  message: string
  tool_count: number
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function invalid(): never {
  throw new ApiError('连接器数据格式不正确', 502)
}

function parseView(value: unknown): ConnectorView {
  if (!record(value) || typeof value.key !== 'string' || typeof value.kind !== 'string'
    || typeof value.name !== 'string' || typeof value.enabled !== 'boolean'
    || typeof value.status !== 'string' || typeof value.tool_count !== 'number') return invalid()
  return value as unknown as ConnectorView
}

function parseDetail(value: unknown): ConnectorDetail {
  if (!record(value) || typeof value.key !== 'string' || typeof value.name !== 'string'
    || !Array.isArray(value.config_fields) || !Array.isArray(value.tools)) return invalid()
  if (value.personal_account != null) parsePersonalAccount(value.personal_account)
  return value as unknown as ConnectorDetail
}

export async function listConnectors(): Promise<ConnectorView[]> {
  const body = await requestJson('/api/connectors')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseView)
}

export async function getConnector(key: string): Promise<ConnectorDetail> {
  return parseDetail(await requestJson('/api/connectors/' + encodeURIComponent(key)))
}

export async function getYangJiBaoHoldings(): Promise<YangJiBaoPortfolio> {
  const body = await requestJson('/api/connectors/yangjibao/holdings', { cache: 'no-store' }, HOLDINGS_SYNC_TIMEOUT_MS)
  if (!record(body) || typeof body.synced_at !== 'string' || typeof body.fund_count !== 'number'
    || !Array.isArray(body.funds)) return invalid()
  for (const item of body.funds) {
    if (!record(item) || typeof item.code !== 'string' || typeof item.name !== 'string') return invalid()
    for (const value of [item.holding_amount, item.estimated_change_percent, item.latest_nav_change_percent]) {
      if (value !== null && (typeof value !== 'number' || !Number.isFinite(value))) return invalid()
    }
    for (const value of [
      item.share, item.cost_amount, item.cost_nav, item.estimated_amount,
      item.latest_nav_amount, item.estimated_today_profit_amount,
    ]) {
      if (value != null && (typeof value !== 'number' || !Number.isFinite(value))) return invalid()
    }
    for (const value of [item.quote_time, item.nav_date]) {
      if (value !== null && typeof value !== 'string') return invalid()
    }
  }
  return body as unknown as YangJiBaoPortfolio
}

export async function updateConnectorConfig(
  key: string,
  config: Record<string, unknown>,
  secrets: Record<string, string>,
): Promise<ConnectorDetail> {
  return parseDetail(await requestJson('/api/admin/connectors/' + encodeURIComponent(key) + '/config', {
    method: 'PUT',
    body: JSON.stringify({ config, secrets }),
  }))
}

export async function setConnectorEnabled(key: string, enabled: boolean): Promise<ConnectorDetail> {
  return parseDetail(await requestJson('/api/admin/connectors/' + encodeURIComponent(key) + '/enabled', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  }))
}

export async function testConnector(key: string): Promise<ConnectorTestResult> {
  const body = await requestJson('/api/admin/connectors/' + encodeURIComponent(key) + '/test', {
    method: 'POST',
  }, CONNECTOR_TEST_TIMEOUT_MS)
  if (!record(body) || typeof body.status !== 'string') return invalid()
  return body as unknown as ConnectorTestResult
}

export interface BuiltinConnector {
  key: string
  kind: ConnectorKind
  name: string
  description: string
  icon: string
  installed: boolean
  config_fields: ConfigField[]
}

function parseBuiltin(value: unknown): BuiltinConnector {
  if (!record(value) || typeof value.key !== 'string' || typeof value.name !== 'string'
    || typeof value.installed !== 'boolean') return invalid()
  return value as unknown as BuiltinConnector
}

export async function listBuiltinConnectors(): Promise<BuiltinConnector[]> {
  const body = await requestJson('/api/connectors-builtin')
  if (!Array.isArray(body)) return invalid()
  return body.map(parseBuiltin)
}

export async function installConnector(key: string): Promise<ConnectorDetail> {
  return parseDetail(await requestJson('/api/admin/connectors/' + encodeURIComponent(key) + '/install', {
    method: 'POST',
  }))
}

export interface AuthResult {
  auth_url: string
  qr_content?: string
  output: string
  already_logged_in: boolean
  state: 'starting' | 'awaiting_user' | 'completed' | 'expired' | 'failed'
  expires_at: string | null
}

export async function authenticateConnector(key: string): Promise<AuthResult> {
  const body = await requestJson('/api/connectors/' + encodeURIComponent(key) + '/my-account/auth', {
    method: 'POST',
  }, CONNECTOR_AUTH_TIMEOUT_MS)
  if (!record(body)) return invalid()
  return body as unknown as AuthResult
}

export async function getConnectorAuthStatus(key: string): Promise<AuthResult> {
  const path = '/api/connectors/' + encodeURIComponent(key) + '/my-account/auth/status'
  const body = key === 'yangjibao'
    ? await requestJson(path, { method: 'POST' }, CONNECTOR_QR_POLL_TIMEOUT_MS)
    : await requestJson(path)
  if (!record(body)) return invalid()
  return body as unknown as AuthResult
}

export function connectorHealthLabel(connector: ConnectorHealth & { enabled: boolean; status: ConnectorStatus; key?: string }): string {
  if (!connector.enabled) return '未启用'
  if (isPersonalConnector(connector.key)) return '按个人账号连接'
  if (connector.auth_action === 'configure') return '需要配置'
  if (connector.auth_action === 'authorize' || connector.auth_status === 'required') return '需要授权'
  if (connector.last_error_code === 'timeout' || connector.last_error_code === 'check_failed'
    || (connector.auth_status === 'unknown' && !connector.last_error_code)) return '待确认'
  if (connector.status === 'error' || connector.status === 'degraded') return '连接异常'
  if (!connector.last_checked_at || connector.auth_status === 'unknown') return '待确认'
  return connector.status === 'connected' ? '健康' : '待确认'
}

export function connectorActionLabel(connector: ConnectorHealth & { key?: string }): string {
  if (connector.auth_action === 'authorize') return connector.key === 'yangjibao' ? '扫码连接' : '去授权'
  if (connector.auth_action === 'configure') return '配置'
  return '重新检查'
}

export function connectorTime(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '尚无记录'
}
