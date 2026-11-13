import { expect, it } from 'vitest'
import { renderMarkdown, renderCitations } from './markdown'

it('渲染 GFM 表格与基础 Markdown', () => {
  const html = renderMarkdown('| 天 | 知识点 |\n|---|------|\n| 1 | **规划** |')
  expect(html).toContain('<table>')
  expect(html).toContain('<thead>')
  expect(html).toContain('<strong>规划</strong>')
  expect(html).toContain('<td>1</td>')
})

it('清洗脚本、事件属性和不安全链接', () => {
  const html = renderMarkdown('<script>alert(1)</script><img src=x onerror=alert(1)>\n[x](javascript:alert(1))')
  expect(html).not.toContain('<script')
  expect(html).not.toContain('onerror')
  expect(html).not.toContain('<img')
  expect(html).not.toContain('javascript:')
})

it('外部链接在新窗口打开并带安全属性', () => {
  const html = renderMarkdown('[文档](https://example.com/docs)')
  expect(html).toContain('href="https://example.com/docs"')
  expect(html).toContain('target="_blank"')
  expect(html).toContain('rel="noopener noreferrer"')
})

it('保留代码块与换行', () => {
  const html = renderMarkdown('第一行\n第二行\n\n```python\nprint(1)\n```')
  expect(html).toContain('<br>')
  expect(html).toContain('<pre><code class="language-python">')
})

it('renderCitations 把 [n] 渲染为可点击角标', () => {
  const html = renderCitations('<p>参考 [1] 和 [2] 的内容。</p>')
  expect(html).toContain('data-cite="1"')
  expect(html).toContain('data-cite="2"')
  expect(html).toContain('[1]')
  expect(html).toContain('[2]')
})

it('renderCitations 跳过代码块中的 [n]', () => {
  const html = renderCitations('<pre><code>arr[1] = 5</code></pre><p>参考 [1]。</p>')
  // 代码块中的 [1] 保持原样
  expect(html).toContain('arr[1]')
  // 正文中的 [1] 被渲染为角标
  expect(html).toContain('data-cite="1"')
  // 角标只出现一次（正文），不在代码块内
  const citeCount = (html.match(/data-cite="1"/g) ?? []).length
  expect(citeCount).toBe(1)
})

it('renderCitations 不替换越界编号', () => {
  const html = renderCitations('<p>参考 [99] 的内容。</p>', 5)
  expect(html).toContain('[99]')
  expect(html).not.toContain('data-cite="99"')
})
