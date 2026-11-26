import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { activateModelProfile, addModelProfile, listModelProfiles, testModelProfile, type ModelProfile } from '../api/config'
import ModelProfilesPanel from './ModelProfilesPanel.vue'

vi.mock('../api/config', () => ({
  activateModelProfile: vi.fn(), addModelProfile: vi.fn(), listModelProfiles: vi.fn(), testModelProfile: vi.fn(),
}))

it('搜索供应商、测试当前模型并单独保存和启用', async () => {
  const payload = {
    name: '团队模型', provider: 'openai_compatible' as const, model_id: 'fixture-text',
    base_url: 'https://tokenhub-intl.tencentmaas.com/v1', vision_model_id: '',
  }
  const saved: ModelProfile = { ...payload, id: 'fixture', active: false, has_api_key: true }
  vi.mocked(listModelProfiles).mockResolvedValue([])
  vi.mocked(testModelProfile).mockResolvedValue([{ check: 'text', ok: true, message: '调用成功' }])
  vi.mocked(addModelProfile).mockResolvedValue(saved)
  vi.mocked(activateModelProfile).mockResolvedValue([])
  const view = mount(ModelProfilesPanel, { props: { disabled: false, revision: 0 }, attachTo: document.body })
  await flushPromises()
  const button = (label: string) => {
    const match = view.findAll('button').find(item => item.text() === label)
    if (!match) throw new Error(`缺少按钮 ${label}`)
    return match
  }
  try {
    const dialog = view.get('dialog')
    Object.assign(dialog.element, {
      showModal() { dialog.element.open = true },
      close() { dialog.element.open = false; dialog.element.dispatchEvent(new Event('close')) },
    })
    await button('添加模型').trigger('click')
    expect(dialog.element.open).toBe(true)
    const input = (label: string) => {
      const match = dialog.findAll('label').find(item => item.text().startsWith(label))
      if (!match) throw new Error(`缺少输入框 ${label}`)
      return match.get<HTMLInputElement>('input')
    }
    await input('API Key').setValue('other-provider-key')
    await dialog.get('#model-provider-search').setValue('新加坡')
    expect(dialog.findAll('.model-profiles__preset').map(item => item.text())).toEqual(['腾讯云 TokenHub（新加坡）'])
    await button('腾讯云 TokenHub（新加坡）').trigger('click')
    expect(input('Base URL').element.value).toBe(payload.base_url)
    expect(input('API Key').element.value).toBe('')
    expect(input('API Key').attributes('type')).toBe('password')
    await input('显示名称').setValue(payload.name)
    await input('模型 ID').setValue(payload.model_id)
    await input('API Key').setValue('fixture-secret')
    await button('测试连接').trigger('click')
    await flushPromises()
    expect(testModelProfile).toHaveBeenCalledWith({ ...payload, api_key: 'fixture-secret' })
    expect(dialog.text()).toContain('文本模型 · 调用成功')
    expect(addModelProfile).not.toHaveBeenCalled()
    await dialog.get('form').trigger('submit')
    await flushPromises()
    expect(addModelProfile).toHaveBeenCalledWith({ ...payload, api_key: 'fixture-secret' })
    expect(dialog.element.open).toBe(false)
    expect(input('API Key').element.value).toBe('')
    expect(activateModelProfile).not.toHaveBeenCalled()
    await button('设为默认').trigger('click')
    await flushPromises()
    expect(activateModelProfile).toHaveBeenCalledWith('fixture')
    expect(view.emitted('activated')).toEqual([[[]]])
    expect(view.get('.model-profiles__current').text()).toBe('默认模型')
  } finally {
    view.unmount()
  }
})
