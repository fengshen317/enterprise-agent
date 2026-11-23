import { flushPromises, mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ChatComposer from './ChatComposer.vue'

function setup(modelValue = '') {
  return mount(ChatComposer, {
    props: { modelValue, contextKey: 'c1', active: false, loading: false, submitting: false, stopping: false, sendMode: 'enter' },
  })
}

it('单独输入斜杠打开模板，选中后只填入草稿', async () => {
  const wrapper = setup()
  await wrapper.get('textarea').setValue('/')
  await wrapper.setProps({ modelValue: '/' })
  expect(wrapper.get('button[type="submit"]').element).toHaveProperty('disabled', true)
  await wrapper.get('textarea').trigger('keydown', { key: 'Enter' })
  expect(wrapper.emitted('send')).toBeUndefined()
  await wrapper.findAll('.chat-composer__panel button')[0]!.trigger('click')
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['请将结果整理成表格，并标明关键差异和缺失信息。'])
  expect(wrapper.find('.chat-composer__panel').exists()).toBe(false)
  wrapper.unmount()
})

it('通过菜单追加模板，不覆盖现有草稿，也可用 Escape 关闭', async () => {
  const wrapper = setup('比较两种方案')
  const toggle = wrapper.get('button[aria-expanded]')
  await toggle.trigger('click')
  await flushPromises()
  await wrapper.get('.chat-composer__panel').trigger('keydown', { key: 'Escape' })
  expect(toggle.attributes('aria-expanded')).toBe('false')
  await toggle.trigger('click')
  await wrapper.findAll('.chat-composer__panel button')[0]!.trigger('click')
  expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatch(/^比较两种方案\n\n请将结果整理成表格/)
  const choice = wrapper.get('select[aria-label="选择发送快捷键"]')
  await choice.setValue('modifier')
  expect(wrapper.emitted('update:sendMode')).toEqual([['modifier']])
  wrapper.unmount()
})

it('支持两种发送快捷键，并阻止中文输入法回车误发送', async () => {
  const wrapper = setup('追问')
  const editor = wrapper.get('textarea')
  await editor.trigger('compositionstart')
  await editor.trigger('keydown', { key: 'Enter' })
  await editor.trigger('compositionend')
  await editor.trigger('keydown', { key: 'Enter', isComposing: true })
  await editor.trigger('keydown', { key: 'Enter', shiftKey: true })
  expect(wrapper.emitted('send')).toBeUndefined()
  await editor.trigger('keydown', { key: 'Enter' })
  expect(wrapper.emitted('send')).toHaveLength(1)
  await wrapper.setProps({ sendMode: 'modifier' })
  await editor.trigger('keydown', { key: 'Enter' })
  expect(wrapper.emitted('send')).toHaveLength(1)
  await editor.trigger('keydown', { key: 'Enter', ctrlKey: true })
  await editor.trigger('keydown', { key: 'Enter', metaKey: true })
  expect(wrapper.emitted('send')).toHaveLength(3)
  await wrapper.setProps({ active: true })
  await editor.trigger('keydown', { key: 'Enter', ctrlKey: true })
  expect(wrapper.emitted('send')).toHaveLength(3)
  wrapper.unmount()
})
