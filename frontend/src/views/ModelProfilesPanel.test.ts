import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import {
  activateModelProfile, addModelProfile, discoverModelCatalog, listModelProfiles, listModelProviders, testModelProfile,
  type ModelProfile, type ModelProviderPreset,
} from '../api/config'
import ModelProfilesPanel from './ModelProfilesPanel.vue'

vi.mock('../api/config', () => ({
  activateModelProfile: vi.fn(), addModelProfile: vi.fn(), discoverModelCatalog: vi.fn(),
  listModelProfiles: vi.fn(), listModelProviders: vi.fn(), testModelProfile: vi.fn(),
}))

const mimo: ModelProviderPreset = {
  id: 'mimo', label: '小米 MiMo', base_url: 'https://api.xiaomimimo.com/v1',
  docs_url: 'https://mimo.mi.com/docs/zh-CN/api/model/list-models', supports_discovery: true,
}
const deepseek: ModelProviderPreset = {
  id: 'deepseek', label: 'DeepSeek', base_url: 'https://api.deepseek.com',
  docs_url: 'https://api-docs.deepseek.com/api/list-models/', supports_discovery: true,
}

beforeEach(() => {
  vi.mocked(listModelProfiles).mockResolvedValue([])
  vi.mocked(listModelProviders).mockResolvedValue([mimo, deepseek])
  vi.mocked(testModelProfile).mockResolvedValue([{ check: 'text', ok: true, message: '调用成功' }])
  vi.mocked(activateModelProfile).mockResolvedValue([])
})

async function setup() {
  const view = mount(ModelProfilesPanel, {
    props: { disabled: false, revision: 0, currentModel: '当前部署模型' }, attachTo: document.body,
  })
  const button = (label: string) => {
    const match = view.findAll('button').find(item => item.text() === label)
    if (!match) throw new Error('缺少按钮 ' + label)
    return match
  }
  await flushPromises()
  await button('添加模型').trigger('click')
  await flushPromises()
  return { view, button, supplier: view.get('#model-supplier') }
}

it('小米 MiMo 只需 API Key 即可加载模型，保存和启用分别操作', async () => {
  const payload = {
    name: '小米 MiMo · fixture-pro', provider: 'openai_compatible' as const, model_id: 'fixture-pro',
    base_url: mimo.base_url, vision_model_id: '',
  }
  const saved: ModelProfile = { ...payload, id: 'fixture', active: false, has_api_key: true }
  vi.mocked(discoverModelCatalog).mockResolvedValue(['fixture-flash', 'fixture-pro'])
  vi.mocked(addModelProfile).mockResolvedValue(saved)
  const { view, button, supplier } = await setup()
  try {
    expect(view.get('.model-profiles__default').text()).toContain('当前部署模型')
    await supplier.setValue('deepseek')
    await view.get('#model-api-key').setValue('other-provider-key')
    await supplier.setValue('mimo')
    const key = view.get<HTMLInputElement>('#model-api-key')
    expect(key.element.value).toBe('')
    expect(key.attributes('type')).toBe('password')
    expect(view.find('input[type="url"]').exists()).toBe(false)
    await key.setValue('fixture-secret')
    await button('获取模型').trigger('click')
    await flushPromises()
    expect(discoverModelCatalog).toHaveBeenCalledExactlyOnceWith('mimo', 'fixture-secret')
    const model = view.findAll('select')[1]
    await model.setValue('fixture-pro')
    await button('测试连接').trigger('click')
    await flushPromises()
    expect(testModelProfile).toHaveBeenCalledWith({ ...payload, api_key: 'fixture-secret' })
    expect(view.text()).toContain('文本模型 · 调用成功')
    expect(addModelProfile).not.toHaveBeenCalled()
    await view.get('form').trigger('submit')
    await flushPromises()
    expect(addModelProfile).toHaveBeenCalledWith({ ...payload, api_key: 'fixture-secret' })
    expect(view.find('#model-api-key').exists()).toBe(false)
    expect(activateModelProfile).not.toHaveBeenCalled()
    await button('设为默认').trigger('click')
    await flushPromises()
    expect(activateModelProfile).toHaveBeenCalledWith('fixture')
    expect(view.emitted('activated')).toEqual([[[]]])
    expect(view.get('.model-profiles__default').text()).toContain(saved.model_id)
    expect(view.get('.model-profiles__default').text()).toContain('小米 MiMo')
  } finally {
    view.unmount()
  }
})

it('目录不可用时可以手动填写，切换自定义接口会清空上一家厂商的凭据', async () => {
  vi.mocked(discoverModelCatalog).mockRejectedValueOnce(new Error('厂商拒绝请求'))
  const { view, button, supplier } = await setup()
  try {
    await supplier.setValue('mimo')
    await view.get('#model-api-key').setValue('fixture-secret')
    await button('获取模型').trigger('click')
    await flushPromises()
    expect(view.get('[role="alert"]').text()).toContain('厂商拒绝请求')
    await button('手动填写模型 ID').trigger('click')
    expect(view.get<HTMLInputElement>('input[maxlength="200"]').element.value).toBe('')
    await supplier.setValue('custom')
    expect(view.get<HTMLInputElement>('#model-api-key').element.value).toBe('')
    expect(view.get<HTMLInputElement>('input[type="url"]').element.value).toBe('')
  } finally {
    view.unmount()
  }
})
