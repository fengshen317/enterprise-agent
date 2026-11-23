import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../../api/knowledgeModel'
import KnowledgeModelSettingsView from './KnowledgeModelSettingsView.vue'

vi.mock('../../api/knowledgeModel')

describe('知识库模型设置', () => {
  beforeEach(() => {
    vi.mocked(api.getEmbeddingStatus).mockResolvedValue({
      state: 'needs_model', model: null, environment_override: '',
    })
  })

  it('验证后显示默认模型并清空供应商密钥输入', async () => {
    vi.mocked(api.saveEmbedding).mockResolvedValue({
      state: 'ready',
      model: { name: 'BAAI/bge-m3', provider: 'SILICONFLOW', instance: 'fixture-instance' },
      environment_override: '',
    })
    const view = mount(KnowledgeModelSettingsView, { global: { stubs: ['RouterLink'] } })
    await flushPromises()
    await view.get('input[type=password]').setValue('fixture-key')
    await view.get('form').trigger('submit')
    await flushPromises()
    expect(api.saveEmbedding).toHaveBeenCalledWith({ preset: 'siliconflow_bge_m3', api_key: 'fixture-key' })
    expect(view.text()).toContain('已启用')
    expect((view.get('input[type=password]').element as HTMLInputElement).value).toBe('')
  })

  it('部署固定模型时隐藏更换表单', async () => {
    vi.mocked(api.getEmbeddingStatus).mockResolvedValue({
      state: 'ready', model: null, environment_override: 'fixed-model',
    })
    const view = mount(KnowledgeModelSettingsView, { global: { stubs: ['RouterLink'] } })
    await flushPromises()
    expect(view.text()).toContain('fixed-model')
    expect(view.find('form').exists()).toBe(false)
  })
})
