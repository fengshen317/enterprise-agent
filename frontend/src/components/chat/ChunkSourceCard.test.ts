import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ChunkSourceCard, { type SourceChunk } from './ChunkSourceCard.vue'

const chunks: SourceChunk[] = [
  { index: 1, content: '员工年假制度规定…', document_name: '员工手册.pdf', similarity: 0.87 },
  { index: 2, content: '考勤管理规定…', document_name: '制度汇编.pdf', similarity: 0.76 },
]

it('渲染来源卡片与相似度', () => {
  const wrapper = mount(ChunkSourceCard, { props: { chunks, collapsible: false } })
  expect(wrapper.text()).toContain('[1]')
  expect(wrapper.text()).toContain('员工手册.pdf')
  expect(wrapper.text()).toContain('87.0%')
  expect(wrapper.text()).toContain('员工年假制度规定…')
  expect(wrapper.text()).toContain('[2]')
  expect(wrapper.text()).toContain('制度汇编.pdf')
  wrapper.unmount()
})

it('空数组不渲染', () => {
  const wrapper = mount(ChunkSourceCard, { props: { chunks: [], collapsible: false } })
  expect(wrapper.find('.chunk-sources').exists()).toBe(false)
  wrapper.unmount()
})

it('可折叠模式默认收起', () => {
  const wrapper = mount(ChunkSourceCard, { props: { chunks, collapsible: true } })
  expect(wrapper.text()).toContain('参考来源 2 条')
  expect(wrapper.find('.chunk-sources__list').isVisible()).toBe(false)
  wrapper.unmount()
})

it('按消息生成不同的来源锚点', () => {
  const wrapper = mount(ChunkSourceCard, { props: { chunks: chunks.slice(0, 1), collapsible: false, anchorPrefix: 'm9' } })
  expect(wrapper.get('article').attributes('id')).toBe('source-m9-1')
  wrapper.unmount()
})

it('相似度为 null 时显示未知', () => {
  const wrapper = mount(ChunkSourceCard, {
    props: { chunks: [{ index: 1, content: 'x', document_name: 'a.pdf', similarity: null }], collapsible: false },
  })
  expect(wrapper.text()).toContain('未知')
  wrapper.unmount()
})
