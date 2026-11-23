import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { listConfigs, updateConfigs, type ConfigItem } from '../api/config'
import ApiSettingsView from './ApiSettingsView.vue'

vi.mock('../api/config', () => ({ listConfigs: vi.fn(), updateConfigs: vi.fn() }))

it('提交兼容 LLM 配置并清除输入框中的密钥', async () => {
  function item(key: string, value: string, isSecret = false): ConfigItem {
    return { key, value_masked: value, is_secret: isSecret, description: '', updated_at: null, updated_by: null }
  }
  vi.mocked(listConfigs).mockResolvedValue([
    item('llm_provider', 'deepseek'),
    item('llm_base_url', 'https://api.example.invalid/v1'),
    item('llm_api_key', '', true),
  ])
  vi.mocked(updateConfigs).mockResolvedValue([
    item('llm_provider', 'openai_compatible'),
    item('llm_base_url', 'https://api.example.invalid/v1'),
    item('llm_model', 'fixture-model'),
    item('llm_api_key', '已配置', true),
  ])
  const view = mount(ApiSettingsView, { global: { stubs: ['RouterLink'] } })
  await flushPromises()
  await view.get('select').setValue('openai_compatible')
  const form = view.findAll('form')[0]
  await form.get('input[placeholder="供应商提供的模型标识"]').setValue('fixture-model')
  await form.get('input[type="password"]').setValue('fixture-secret')
  await form.trigger('submit')
  await flushPromises()
  expect(updateConfigs).toHaveBeenCalledWith(expect.arrayContaining([
    { key: 'llm_provider', value: 'openai_compatible' },
    { key: 'llm_model', value: 'fixture-model' },
    { key: 'llm_api_key', value: 'fixture-secret' },
  ]))
  expect((form.get('input[type="password"]').element as HTMLInputElement).value).toBe('')
  expect(view.text()).toContain('后续调用使用新配置')
  view.unmount()
})
