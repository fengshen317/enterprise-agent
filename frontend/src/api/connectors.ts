import { ApiError, requestJson } from './client'

export type ConnectorKind = 'cli' | 'mcp' | 'api'
export type ConnectorStatus = 'disabled' | 'connected' | 'degraded' | 'error'

export interface ConnectorView {
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

export interface ConnectorDetail {
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
}

export interface ConnectorTestResult {
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
  })
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
  output: string
  already_logged_in: boolean
}

export async function authenticateConnector(key: string): Promise<AuthResult> {
  const body = await requestJson('/api/admin/connectors/' + encodeURIComponent(key) + '/auth', {
    method: 'POST',
  })
  if (!record(body)) return invalid()
  return body as unknown as AuthResult
}
