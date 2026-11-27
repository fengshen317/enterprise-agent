import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import ApprovalCard from './ApprovalCard.vue'
import * as api from '../../api/approvals'

vi.mock('../../api/approvals', () => ({ decideApproval: vi.fn() }))

const approval: api.Approval = {
  id: 'ap1', run_id: 'r1', tool_call_id: 't1', tool_name: 'create_scheduled_task', tool_version: 1,
  risk_level: 'write', args_preview: { summary: '创建定时任务「每周复盘」', cron: '0 9 * * 1' }, status: 'pending',
  requested_at: '2026-11-12T00:00:00Z', expires_at: new Date(Date.now() + 120000).toISOString(), decided_at: null, decision_comment: null,
}

beforeEach(() => vi.resetAllMocks())

it('展示影响说明并批准执行', async () => {
  const approved = { ...approval, status: 'approved' as const }
  vi.mocked(api.decideApproval).mockResolvedValue(approved)
  const pending = {
    ...approval,
    args_preview: { ...approval.args_preview, push_to_wechat: false, push_channels: ['dingtalk', 'feishu'] },
  }
  const wrapper = mount(ApprovalCard, { props: { approval: pending, runId: 'r1' } })
  expect(wrapper.text()).toContain('创建定时任务「每周复盘」')
  expect(wrapper.text()).toContain('推送渠道')
  expect(wrapper.text()).toContain('钉钉、飞书')
  expect(wrapper.text()).not.toContain('微信推送')
  expect(wrapper.text()).toContain('批准并执行')
  await wrapper.get('.approval__approve').trigger('click')
  await flushPromises()
  expect(api.decideApproval).toHaveBeenCalledWith('r1', 'ap1', 'approve')
  expect(wrapper.emitted('decided')?.[0]).toEqual([approved])
  wrapper.unmount()
})

it('拒绝只提交决策，不执行工具', async () => {
  const rejected = { ...approval, status: 'rejected' as const }
  vi.mocked(api.decideApproval).mockResolvedValue(rejected)
  const wrapper = mount(ApprovalCard, { props: { approval, runId: 'r1' } })
  await wrapper.get('.approval__reject').trigger('click')
  await flushPromises()
  expect(api.decideApproval).toHaveBeenCalledWith('r1', 'ap1', 'reject')
  wrapper.unmount()
})

it('展示完整技能正文和结构化权限预览，并保留新增字段', () => {
  const wrapper = mount(ApprovalCard, { props: { runId: 'r1', approval: {
    ...approval, tool_name: 'publish_private_skill', args_preview: {
      version: 2, content_md: '# 复核步骤', knowledge_scope: { mode: 'selected', kb_ids: ['kb1'] },
      tool_keys: [], visibility: 'private', extra_field: false,
    },
  } } })
  for (const text of ['发布私有技能版本', '技能正文', '# 复核步骤', 'kb1', '仅自己可见', 'extra_field', '否']) {
    expect(wrapper.text()).toContain(text)
  }
  expect(wrapper.text()).not.toContain('[object Object]')
  wrapper.unmount()
})
