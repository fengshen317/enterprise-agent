import type { ToolCall } from '../../api/tools'

const actions = new Map([
  ['create_deliverable', '生成可下载文件'],
  ['create_private_skill', '创建私有技能草稿'],
  ['publish_private_skill', '发布私有技能版本'],
  ['create_private_agent', '创建个人 Agent'],
  ['create_scheduled_task', '创建定时任务'],
  ['update_scheduled_task', '修改定时任务'],
])

export function authoringAction(toolName: string): string | undefined {
  return actions.get(toolName)
}

interface ObjectLink { to: string; label: string }

const objectId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function createdObjectLink(call: ToolCall): ObjectLink | undefined {
  if (call.status !== 'completed' || !call.result || !actions.has(call.tool_name)) return
  try {
    const result = JSON.parse(call.result)
    const schedule = call.tool_name === 'create_scheduled_task' || call.tool_name === 'update_scheduled_task'
    if (schedule && result?.schedule_deleted === true) return
    const id = schedule ? result?.schedule_id ?? result?.id : result?.id
    if (typeof id !== 'string' || !objectId.test(id)) return

    // 站内路径来自工具契约，审计结果只提供对象标识与显示名称。
    const name = typeof result.name === 'string' && result.name.trim() ? '「' + result.name + '」' : ''
    if (call.tool_name === 'create_deliverable') return
    if (schedule) return { to: '/schedules?schedule=' + id, label: '查看定时任务' + name }
    if (call.tool_name === 'create_private_agent') return { to: '/agents/' + id, label: '查看个人 Agent' + name }
    const version = Number.isInteger(result.version) && result.version > 0 ? ' · v' + result.version : ''
    return { to: '/capabilities/skills/' + id, label: '查看私有技能' + name + version }
  } catch { return }
}
