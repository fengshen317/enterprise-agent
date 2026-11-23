import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { checkImaAccount, removePersonalAccount, savePersonalAccount } from '../../api/connectors'
import { copyDiagnosticId } from '../../utils/diagnosticClipboard'
import PersonalConnectorAccount from './PersonalConnectorAccount.vue'

vi.mock('../../api/connectors', () => ({ checkImaAccount: vi.fn(), savePersonalAccount: vi.fn(), removePersonalAccount: vi.fn() }))
vi.mock('../../utils/diagnosticClipboard', () => ({ copyDiagnosticId: vi.fn() }))
beforeEach(() => { vi.resetAllMocks() })

it('成员保存凭证后清空输入，停用后仍可解绑', async () => {
  const account = {
    bound: false, guide_url: 'https://ima.qq.com/agent-interface',
    fields: [{ key: 'api_key', label: 'API Key', secret: true }],
  }
  const wrapper = mount(PersonalConnectorAccount, { props: { connectorKey: 'ima', account, enabled: true } })
  vi.mocked(savePersonalAccount).mockResolvedValue({ ...account, bound: true })
  const input = wrapper.get('input')
  expect(input.attributes('type')).toBe('password')
  expect(input.attributes('placeholder')).toBeUndefined()
  await input.setValue('test-only')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(savePersonalAccount).toHaveBeenCalledWith('ima', { api_key: 'test-only' })
  expect(input.element.value).toBe('')
  expect(wrapper.emitted('updated')?.[0]).toEqual([{ ...account, bound: true }])
  await wrapper.setProps({ account: { ...account, bound: true } })
  expect(input.attributes('placeholder')).toBe('••••••••')
  expect(input.element.value).toBe('')

  await wrapper.setProps({ enabled: false })
  expect(wrapper.find('form').exists()).toBe(false)
  vi.mocked(removePersonalAccount).mockResolvedValue(account)
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(removePersonalAccount).toHaveBeenCalledWith('ima')
  expect(wrapper.emitted('updated')?.[1]).toEqual([account])
  wrapper.unmount()
})

it('只为已启用且已绑定的 ima 展示检查，并分别显示结果和失败诊断编号', async () => {
  const account = { bound: true, guide_url: 'https://ima.qq.com/agent-interface', fields: [] }
  const wrapper = mount(PersonalConnectorAccount, { props: { connectorKey: 'ima', account, enabled: true } })
  const diagnosticId = 'bf45eba9-a97a-43af-b112-2d091f9e84d6'
  vi.mocked(checkImaAccount).mockResolvedValue({
    knowledge_base: { status: 'success', error_code: null, diagnostic_id: null },
    notes: { status: 'error', error_code: 'permission_denied', diagnostic_id: diagnosticId },
  })
  vi.mocked(copyDiagnosticId).mockResolvedValue('诊断编号已复制')

  const check = wrapper.findAll('button').find(button => button.text() === '检查连接')!
  await check.trigger('click')
  await flushPromises()
  expect(checkImaAccount).toHaveBeenCalledWith()
  const result = wrapper.get('[aria-label="ima 连通性结果"]')
  expect(result.text()).toContain('知识库 · 连接正常')
  expect(result.text()).toContain('笔记 · 访问权限不足')
  expect(result.get('code').text()).toBe(diagnosticId)
  await result.get('button').trigger('click')
  await flushPromises()
  expect(copyDiagnosticId).toHaveBeenCalledWith(diagnosticId, result.get('code').element)
  expect(wrapper.text()).toContain('诊断编号已复制')

  await wrapper.setProps({ enabled: false })
  expect(wrapper.text()).not.toContain('检查连接')
  expect(wrapper.text()).not.toContain(diagnosticId)
  await wrapper.setProps({ enabled: true, account: { ...account, bound: false } })
  expect(wrapper.text()).not.toContain('检查连接')
  await wrapper.setProps({ connectorKey: 'qq_mail', account })
  expect(wrapper.text()).not.toContain('检查连接')
  wrapper.unmount()
})

it('重新打开已绑定账号时只显示固定掩码，提交时使用新输入的完整凭证', async () => {
  const account = {
    bound: true, guide_url: 'https://example.com/guide',
    fields: [
      { key: 'email', label: 'QQ 邮箱', secret: false },
      { key: 'authorization_code', label: '授权码', secret: true },
    ],
  }
  const wrapper = mount(PersonalConnectorAccount, { props: { connectorKey: 'qq_mail', account, enabled: true } })
  const email = wrapper.get<HTMLInputElement>('input[type="email"]')
  const secret = wrapper.get<HTMLInputElement>('input[type="password"]')
  const saveButton = wrapper.get<HTMLButtonElement>('button[type="submit"]')
  expect(email.attributes('placeholder')).toBeUndefined()
  expect(secret.attributes('placeholder')).toBe('••••••••')
  expect(secret.element.value).toBe('')
  expect(saveButton.element.disabled).toBe(true)
  await email.setValue('example@qq.com')
  await wrapper.get('form').trigger('submit')
  expect(savePersonalAccount).not.toHaveBeenCalled()

  vi.mocked(savePersonalAccount).mockResolvedValueOnce(account)
  await secret.setValue('test-replacement')
  expect(saveButton.element.disabled).toBe(false)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(savePersonalAccount).toHaveBeenCalledWith('qq_mail', {
    email: 'example@qq.com', authorization_code: 'test-replacement',
  })
  expect(secret.element.value).toBe('')
  expect(saveButton.element.disabled).toBe(true)
  await wrapper.setProps({ account: { ...account, bound: false } })
  expect(secret.attributes('placeholder')).toBeUndefined()
  wrapper.unmount()
})
