import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import MarkdownBody from './MarkdownBody.vue'

it('把 Markdown 表格渲染为可读的 HTML 表格', () => {
  const wrapper = mount(MarkdownBody, {
    props: { content: '| 天 | 知识点 |\n|---|------|\n| 1 | 什么是 Deep Agent |' },
  })
  expect(wrapper.find('table').exists()).toBe(true)
  expect(wrapper.findAll('th')).toHaveLength(2)
  expect(wrapper.text()).toContain('什么是 Deep Agent')
  wrapper.unmount()
})

it('危险 HTML 不会进入 DOM', () => {
  const wrapper = mount(MarkdownBody, {
    props: { content: '<script>alert(1)</script><img src=x onerror=alert(1)>安全内容' },
  })
  expect(wrapper.find('script').exists()).toBe(false)
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.text()).toContain('安全内容')
  wrapper.unmount()
})

it('内容更新后重新渲染', async () => {
  const wrapper = mount(MarkdownBody, { props: { content: '第一版' } })
  await wrapper.setProps({ content: '| A |\n|---|\n| 第二版 |' })
  expect(wrapper.find('table').exists()).toBe(true)
  expect(wrapper.text()).toContain('第二版')
  wrapper.unmount()
})
