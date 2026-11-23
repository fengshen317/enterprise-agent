import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { expect, it, vi } from 'vitest'
import * as api from '../api/materials'
import MaterialsView from './MaterialsView.vue'

vi.mock('../api/materials', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/materials')>()
  return { ...original, listMaterials: vi.fn(), getMaterial: vi.fn(), uploadMaterials: vi.fn() }
})

it('检索材料后可打开原件并把已归档材料附到新对话', async () => {
  const item: api.Material = {
    id: 'm1', filename: '项目记录.txt', media_type: 'text/plain', size_bytes: 96,
    text_available: true, text_truncated: false, created_at: '2026-11-19T06:00:00Z',
    source_conversation_id: 'c1', excerpt: '芒草是项目代号。',
  }
  vi.mocked(api.listMaterials).mockResolvedValue({ items: [item], has_more: false })
  vi.mocked(api.getMaterial).mockResolvedValue(item)
  vi.mocked(api.uploadMaterials).mockResolvedValue([item])
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/materials/:materialId?', component: MaterialsView }, { path: '/chat/:conversationId?', component: { template: '<div />' } }] })
  await router.push('/materials')
  const wrapper = mount(MaterialsView, { global: { plugins: [router, createPinia()] } })
  await flushPromises()
  await wrapper.get('input[type="search"]').setValue('芒草')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(api.listMaterials).toHaveBeenLastCalledWith('芒草', 0, 20)
  expect(wrapper.text()).toContain('芒草是项目代号')
  await wrapper.get('a[href="/materials/m1"]').trigger('click')
  await flushPromises()
  const detail = wrapper.get('[aria-label="所选材料"]')
  expect(detail.get('pre').text()).toBe(item.excerpt)
  expect(detail.get('a[href^="/api/materials/m1/file?"]').attributes('href')).toContain('download=true')
  expect(detail.get('a[href="/chat?material=m1"]').text()).toContain('附到新对话')
  const picker = wrapper.get('input[type="file"]')
  const file = new File(['附录'], '附录.txt', { type: 'text/plain' })
  Object.defineProperty(picker.element, 'files', { configurable: true, value: [file] })
  await picker.trigger('change')
  await flushPromises()
  expect(api.uploadMaterials).toHaveBeenCalledWith([file])
  expect(wrapper.get('[role="status"]').text()).toContain('已归档 1 份材料')
  wrapper.unmount()
})
