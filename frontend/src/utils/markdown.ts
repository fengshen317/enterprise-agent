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

export function renderCitations(html: string, allowed: readonly number[]): { html: string; cited: number[] } {
  if (!allowed.length) return { html, cited: [] }
  const template = document.createElement('template')
  template.innerHTML = html
  const walk = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT)
  const nodes: Node[] = []
  const valid = new Set(allowed)
  const cited = new Set<number>()
  while (walk.nextNode()) nodes.push(walk.currentNode)
  for (const node of nodes) {
    if (node.parentElement?.closest('a, code, pre, button')) continue
    const text = node.textContent ?? ''
    let offset = 0
    const fragment = document.createDocumentFragment()
    for (const match of text.matchAll(/\[([1-9]\d*)\]/g)) {
      const index = Number(match[1])
      if (!valid.has(index)) continue
      cited.add(index)
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'citation'
      button.dataset.cite = String(index)
      button.setAttribute('aria-label', '查看引用 ' + index)
      button.textContent = match[0]
      fragment.append(text.slice(offset, match.index), button)
      offset = match.index + match[0].length
    }
    if (offset) {
      fragment.append(text.slice(offset))
      node.parentNode?.replaceChild(fragment, node)
    }
  }
  return { html: template.innerHTML, cited: [...cited] }
}
