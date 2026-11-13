import DOMPurify from 'dompurify'
import { marked } from 'marked'

// 模型回答可能包含原始 HTML；统一先转 Markdown，再用白名单清洗。
const FORBID_TAGS = [
  'img',
  'svg',
  'math',
  'iframe',
  'object',
  'embed',
  'style',
  'form',
  'input',
  'button',
  'textarea',
  'select',
  'option',
  'video',
  'audio',
  'canvas',
]
const FORBID_ATTR = ['style', 'srcset', 'formaction']

DOMPurify.addHook('afterSanitizeAttributes', node => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, char => {
    const entities: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }
    return entities[char] ?? char
  })
}

export function renderMarkdown(source: string): string {
  try {
    const html = marked.parse(source, { async: false, gfm: true, breaks: true }) as string
    return String(DOMPurify.sanitize(html, { USE_PROFILES: { html: true }, FORBID_TAGS, FORBID_ATTR }))
  } catch {
    // 极端情况下退化为纯文本，保证消息仍然可读且不注入 HTML。
    return escapeHtml(source)
  }
}

/** 把正文中的 [n] 引用编号渲染为可点击角标，跳过代码块和链接。 */
export function renderCitations(html: string, maxCite = 99): string {
  // 按代码块分割，只在非代码段替换 [n]。
  const segments = html.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g)
  return segments.map(segment => {
    // 代码块原样保留
    if (segment.startsWith('<pre') || segment.startsWith('<code')) return segment
    // 跳过已有的 <a> 链接内容中的 [n]（如 [1](url) 已被 marked 解析为 <a>）
    return segment.replace(/\[(\d{1,2})\]/g, (match, num: string) => {
      const n = parseInt(num, 10)
      if (n < 1 || n > maxCite) return match
      return `<sup class="citation" data-cite="${n}" style="cursor:pointer;color:#18775c;font-size:0.75em;">[${n}]</sup>`
    })
  }).join('')
}
