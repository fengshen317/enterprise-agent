import { logDetailKeys, type LogComponent, type LogDetailKey, type LogLevel, type LogOutcome, type SystemLogEvent } from '../api/systemLogs'

export const componentLabels: Record<LogComponent, string> = {
  http: 'HTTP 请求', run: 'Agent 运行', tool: '工具调用', model: '模型',
  connector: '连接器', schedule: '定时任务', wechat: '微信', feishu: '飞书', dingtalk: '钉钉', qq: 'QQ',
}
export const outcomeLabels: Record<LogOutcome, string> = { error: '失败', warning: '警告', success: '成功' }
export const levelLabels: Record<LogLevel, string> = { info: '信息', warning: '警告', error: '错误' }

const eventLabels = new Map<string, string>([
  ['http_request_accepted', '问答请求已受理'],
  ['http_request_failed', 'HTTP 请求失败'],
  ['run_started', 'Agent 开始运行'],
  ['run_finished', 'Agent 运行结束'],
  ['tool_finished', '工具执行结束'],
  ['model_retry', '模型请求将重试'],
  ['model_finished', '模型请求结束'],
  ['connector_check', '连接器连通性检查'],
  ['schedule_finished', '定时任务执行结束'],
  ['wechat_delivery', '微信消息投递'],
  ['channel_delivery', '企业渠道消息投递'],
])
const phaseLabels = new Map<string, string>([
  ['request_validation', '请求校验'], ['auth', '授权'], ['transport', '网络传输'],
  ['upstream_business', '上游业务'], ['response_validation', '响应校验'],
])
const ruleLabels = new Map<string, string>([
  ['invalid_json', 'JSON 格式'], ['response_envelope', '响应结构'], ['response_data', '响应数据'],
  ['knowledge_base_id', '知识库 ID 字段'], ['knowledge_list', '知识库列表'], ['row_identity', '列表项标识'],
  ['media_type', '媒体类型'], ['note_identity', '笔记标识'], ['note_content', '笔记内容'],
])
const runStatusLabels = new Map<string, string>([['completed', '已完成'], ['failed', '失败'], ['cancelled', '已停止']])
const channelLabels = new Map<string, string>([
  ['wechat', '微信'], ['feishu', '飞书'], ['dingtalk', '钉钉'], ['qq', 'QQ'],
])
const detailValueLabels: Partial<Record<LogDetailKey, ReadonlyMap<string, string>>> = {
  phase: phaseLabels, rule: ruleLabels, run_status: runStatusLabels, channel: channelLabels,
}
const detailLabels: Record<LogDetailKey, string> = {
  phase: '阶段', upstream_status: '上游 HTTP', upstream_code: '业务码', rule: '校验规则',
  route: '接口路径', method: '请求方法', status_code: 'HTTP 状态', attempt: '当前尝试',
  max_attempts: '最多尝试', channel: '渠道', run_status: '运行状态',
}
const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' })
const fullDateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium', hour12: false })
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})
const timelineFormat = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hourCycle: 'h23',
})
const timelineFields = new Set<LogDetailKey>(['upstream_status', 'upstream_code', 'phase', 'rule', 'status_code', 'run_status'])

export function eventTitle(event: SystemLogEvent): string {
  if (event.component === 'connector' && event.outcome !== 'success' && event.details?.phase === 'response_validation') return '连接器响应校验失败'
  return eventLabels.get(event.event) ?? event.event
}

export function sourceDetail(event: SystemLogEvent): string {
  return event.connector_key || event.tool_name || ''
}

export function diagnosticSummary(event: SystemLogEvent): string {
  const phase = event.details?.phase
  if (event.component === 'connector' && phase === 'response_validation' && event.outcome !== 'success') return '上游已响应，适配器校验失败'
  if (typeof phase === 'string' && event.outcome !== 'success') return (phaseLabels.get(phase) ?? phase) + '异常'
  return eventTitle(event)
}

export function diagnosticMetrics(event: SystemLogEvent): { key: LogDetailKey; label: string; value: string }[] {
  return logDetailKeys.flatMap(key => {
    const detail = event.details?.[key]
    if (detail === null || detail === undefined) return []
    const value = String(detail)
    return [{ key, label: detailLabels[key], value: detailValueLabels[key]?.get(value) ?? value }]
  })
}

export function timelineNote(event: SystemLogEvent): string {
  const markers = [sourceDetail(event), event.error_code]
  const metrics = diagnosticMetrics(event).filter(item => timelineFields.has(item.key))
  const { method, route } = event.details ?? {}
  if (method && route) markers.push(method + ' ' + route)
  markers.push(...metrics.map(item => item.key === 'phase' ? item.value : item.label + ' ' + item.value))
  return markers.filter(Boolean).join(' · ')
}

export function dayLabel(value: string): string {
  return dateFormat.format(new Date(value))
}

export function dateTimeLabel(value: string): string {
  return fullDateFormat.format(new Date(value))
}

export function timeLabel(value: string, precise = false): string {
  return (precise ? timelineFormat : timeFormat).format(new Date(value))
}

export function durationLabel(value: number | null): string {
  if (value === null) return '—'
  return value >= 1000 ? Number((value / 1000).toFixed(2)) + ' s' : value + ' ms'
}
