import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import * as api from '../api/personalModels'
import ModelProfilesPanel from './ModelProfilesPanel.vue'

vi.mock('../api/personalModels', () => ({
  listPersonalModels: vi.fn(), personalProviders: vi.fn(), discoverPersonalModels: vi.fn(),
  addPersonalModel: vi.fn(), activatePersonalModel: vi.fn(), followSharedModel: vi.fn(), testPersonalModel: vi.fn(),
}))

it('成员用个人凭据发现模型，保存为本人默认并恢复公共模型', async () => {
  const shared = { name: 'shared', provider: 'deepseek' as const, model_id: 'shared-model', base_url: '', vision_model_id: '' }
  const profile = { id: 'personal-id', name: '小米 MiMo · personal-model', provider: 'openai_compatible' as const,
    model_id: 'personal-model', base_url: 'https://api.xiaomimimo.com/v1', vision_model_id: '', active: true, has_api_key: true }
  vi.mocked(api.listPersonalModels).mockResolvedValue({ profiles: [], shared_default: shared })
  vi.mocked(api.personalProviders).mockResolvedValue([{ id: 'mimo', label: '小米 MiMo', base_url: profile.base_url,
    docs_url: 'https://mimo.mi.com/', supports_discovery: true }])
  vi.mocked(api.discoverPersonalModels).mockResolvedValue([profile.model_id])
  vi.mocked(api.addPersonalModel).mockResolvedValue({ profiles: [profile], shared_default: shared })
  vi.mocked(api.followSharedModel).mockResolvedValue({ profiles: [{ ...profile, active: false }], shared_default: shared })
  const view = mount(ModelProfilesPanel, { props: { personal: true, disabled: false, revision: 0 }, attachTo: document.body })
  const button = (label: string) => {
    const result = view.findAll('button').find(item => item.text() === label)
    if (!result) throw new Error('没有按钮 ' + label)
    return result
  }
  try {
    await flushPromises()
    await button('添加我的模型').trigger('click')
    await flushPromises()
    expect(view.find('option[value="custom"]').exists()).toBe(false)
    await view.get('#model-supplier').setValue('mimo')
    await view.get('#model-api-key').setValue('personal-secret')
    await button('获取模型').trigger('click')
    await flushPromises()
    expect(api.discoverPersonalModels).toHaveBeenCalledWith('mimo', 'personal-secret')
    await view.get('form').trigger('submit')
    await flushPromises()
    expect(api.addPersonalModel).toHaveBeenCalledWith({
      name: profile.name, provider: profile.provider, model_id: profile.model_id,
      base_url: profile.base_url, vision_model_id: '', api_key: 'personal-secret',
    }, true)
    expect(view.get('.model-profiles__default').text()).toContain('personal-model')
    expect(view.find('#model-api-key').exists()).toBe(false)
    await button('空间模型').trigger('click')
    await button('跟随空间默认').trigger('click')
    await flushPromises()
    expect(api.followSharedModel).toHaveBeenCalledOnce()
    expect(view.get('.model-profiles__default').text()).toContain('shared-model')
  } finally { view.unmount() }
})
