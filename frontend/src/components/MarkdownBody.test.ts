import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import MarkdownBody from './MarkdownBody.vue'

afterEach(() => { vi.unstubAllGlobals() })

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

it('只有当前回答可定位的正文引用可用键盘触发，不改写链接和代码', async () => {
  const wrapper = mount(MarkdownBody, {
    props: { content: '依据[1]，排除[8]、[链接 [1]](https://example.com) 和 `a[1]`。', citationIndices: [1] },
  })
  const references = wrapper.findAll('button[data-cite]')
  expect(references).toHaveLength(1)
  expect(wrapper.get('a').text()).toContain('[1]')
  expect(wrapper.get('code').text()).toBe('a[1]')
  expect(wrapper.text()).toContain('[8]')
  await references[0]!.trigger('click')
  expect(wrapper.emitted('cite')).toEqual([[1, references[0]!.element, [1]]])
  wrapper.unmount()
})

it('结束后可逐块复制原代码，未知语言仍可阅读，代码中的标签不会执行', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const wrapper = mount(MarkdownBody, {
    attachTo: document.body,
    props: {
      content: '```js\nconst tag = "<img src=x onerror=alert(1)>"\n```\n\n```custom\n  a[1]\n```',
      citationIndices: [1],
      enhanceBlocks: true,
    },
  })
  await vi.waitFor(() => { expect(wrapper.get('.hljs-keyword').text()).toBe('const') })
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.find('button[data-cite]').exists()).toBe(false)
  expect(wrapper.find('.markdown-body__copy').exists()).toBe(false)

  await wrapper.setProps({ canCopyCode: true })
  const [first, second] = wrapper.findAll('.markdown-body__code')
  expect(first!.get('.markdown-body__code-header').text()).toContain('js')
  expect(second!.get('.markdown-body__code-header').text()).toContain('custom')
  expect(second!.find('code span').exists()).toBe(false)
  await second!.get('button').trigger('click')
  await flushPromises()
  expect(writeText).toHaveBeenCalledExactlyOnceWith('  a[1]\n')
  expect(second!.get('button').text()).toBe('已复制')

  writeText.mockRejectedValueOnce(new Error('剪贴板权限受限'))
  await first!.get('button').trigger('click')
  await flushPromises()
  expect(first!.get('button').text()).toBe('复制失败')
  expect(wrapper.get('[role="status"]').text()).toContain('请手动选中代码')
  expect(second!.get('button').text()).toBe('复制代码')
  wrapper.unmount()
})
