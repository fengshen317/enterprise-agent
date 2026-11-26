import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { listConfigs, testConfigs, updateConfigs, type ConfigItem } from '../api/config'
import ApiSettingsView from './ApiSettingsView.vue'

vi.mock('../api/config', () => ({ listConfigs: vi.fn(), testConfigs: vi.fn(), updateConfigs: vi.fn() }))

function item(key: string, value: string, isSecret = false): ConfigItem {
  return { key, value_masked: value, is_secret: isSecret, description: '', updated_at: null, updated_by: null }
}

it('提交兼容 LLM 配置并清除输入框中的密钥', async () => {
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
  const view = mount(ApiSettingsView, { global: { stubs: ['RouterLink', 'ModelProfilesPanel'] } })
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

it('以当前表单测试文本与图片模型，不提交保存', async () => {
  vi.mocked(updateConfigs).mockClear()
  vi.mocked(listConfigs).mockResolvedValue([
    item('llm_provider', 'openai_compatible'),
    item('llm_base_url', 'https://api.example.invalid/v1'),
    item('llm_model', 'saved-model'),
    item('llm_api_key', '已配置', true),
  ])
  vi.mocked(testConfigs).mockResolvedValue([
    { check: 'text', ok: true, message: '调用成功' },
    { check: 'vision', ok: false, message: '请检查模型能力' },
  ])
  const view = mount(ApiSettingsView, { global: { stubs: ['RouterLink', 'ModelProfilesPanel'] } })
  await flushPromises()
  const form = view.findAll('form')[0]
  const key = form.get<HTMLInputElement>('input[type="password"]')
  expect(key.element.value).toBe('')
  expect(key.attributes('placeholder')).toBe('••••••••')
  await form.get('input[placeholder="供应商提供的模型标识"]').setValue('draft-model')
  await form.get('input[placeholder="留空时使用上方模型"]').setValue('draft-vision')
  await form.get('button.api-settings__test').trigger('click')
  await flushPromises()
  expect(testConfigs).toHaveBeenCalledWith('llm', [
    { key: 'llm_model', value: 'draft-model' }, { key: 'llm_vision_model', value: 'draft-vision' },
  ])
  expect(updateConfigs).not.toHaveBeenCalled()
  expect(form.text()).toContain('文本模型 · 调用成功')
  expect(form.text()).toContain('图片模型 · 请检查模型能力')
  view.unmount()
})

it('单独测试 Tavily，保留密钥草稿且使用密码框', async () => {
  vi.mocked(listConfigs).mockResolvedValue([item('tavily_api_key', '已配置', true)])
  vi.mocked(testConfigs).mockResolvedValue([{ check: 'search', ok: true, message: '搜索成功' }])
  const view = mount(ApiSettingsView, { global: { stubs: ['RouterLink', 'ModelProfilesPanel'] } })
  await flushPromises()
  const form = view.findAll('form')[1]
  const key = form.get<HTMLInputElement>('input[type="password"]')
  await key.setValue('new-search-key')
  await form.get('button.api-settings__test').trigger('click')
  await flushPromises()
  expect(testConfigs).toHaveBeenCalledWith('search', [{ key: 'tavily_api_key', value: 'new-search-key' }])
  expect(key.element.value).toBe('new-search-key')
  expect(form.text()).toContain('Tavily · 搜索成功')
  view.unmount()
})
