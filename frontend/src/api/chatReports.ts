import { ApiError, requestJson } from './client'

export interface ChatReportRef {
  id: string
  title: string
}

export type ReportCell = string | number | boolean | null

export interface ReportChartSpec {
  title: string
  type: 'bar' | 'line'
  x: string
  y: string[]
  unit?: string | null
}

export interface ChatReport extends ChatReportRef {
  explanation: string
  columns: string[]
  rows: ReportCell[][]
  charts: ReportChartSpec[]
  source_files: string[]
  code: string
}

function invalid(): never {
  throw new ApiError('服务返回的报表格式不正确', 502)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string')
}

function isCell(value: unknown): value is ReportCell {
  return value === null || typeof value === 'string' || typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value))
}

export function parseChatReportRef(value: unknown): ChatReportRef {
  if (!isRecord(value) || typeof value.id !== 'string' || !value.id || typeof value.title !== 'string') return invalid()
  return { id: value.id, title: value.title }
}

function parseChart(value: unknown, columns: ReadonlySet<string>): ReportChartSpec {
  if (!isRecord(value) || typeof value.title !== 'string' || (value.type !== 'bar' && value.type !== 'line')
    || typeof value.x !== 'string' || !columns.has(value.x) || !isStringArray(value.y) || !value.y.length
    || !value.y.every(column => columns.has(column))
    || !(value.unit === undefined || value.unit === null || typeof value.unit === 'string')) return invalid()
  return { title: value.title, type: value.type, x: value.x, y: value.y, unit: value.unit }
}

export async function getChatReport(conversationId: string, reportId: string): Promise<ChatReport> {
  const path = '/api/conversations/' + encodeURIComponent(conversationId) + '/reports/' + encodeURIComponent(reportId)
  const body = await requestJson(path)
  const reference = parseChatReportRef(body)
  if (reference.id !== reportId || !isRecord(body) || typeof body.explanation !== 'string'
    || !isStringArray(body.columns) || !Array.isArray(body.rows) || !Array.isArray(body.charts)
    || !isStringArray(body.source_files) || typeof body.code !== 'string') return invalid()

  const columns = new Set(body.columns)
  if (columns.size !== body.columns.length) return invalid()
  const rows = body.rows.map((row: unknown) => {
    if (!Array.isArray(row) || row.length !== columns.size || !row.every(isCell)) return invalid()
    return row
  })
  return {
    ...reference,
    explanation: body.explanation,
    columns: body.columns,
    rows,
    charts: body.charts.map((chart: unknown) => parseChart(chart, columns)),
    source_files: body.source_files,
    code: body.code,
  }
}
