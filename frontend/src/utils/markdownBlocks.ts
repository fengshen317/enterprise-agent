import DOMPurify from 'dompurify'
import type { highlightCode } from './codeHighlight'

const LANGUAGE_CLASS = /^language-([a-z\d.+#_-]{1,32})$/i

export function decorateMarkdownBlocks(sanitizedHtml: string, canCopy: boolean, highlight?: typeof highlightCode): string {
  const template = document.createElement('template')
  template.innerHTML = sanitizedHtml

  for (const pre of template.content.querySelectorAll('pre')) {
    const code = pre.querySelector<HTMLElement>(':scope > code')
    if (!code) continue

    const language = Array.from(code.classList).map(name => LANGUAGE_CLASS.exec(name)?.[1]).find(Boolean)
    const text = code.textContent ?? ''
    const highlighted = language && highlight?.(text, language)
    if (highlighted) {
      code.innerHTML = DOMPurify.sanitize(highlighted, { ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'] })
    }

    const block = document.createElement('div')
    block.className = 'markdown-body__code'
    const header = document.createElement('div')
    header.className = 'markdown-body__code-header'
    const label = document.createElement('span')
    label.textContent = language ?? '代码'
    header.append(label)
    if (canCopy && text.trim()) {
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'markdown-body__copy'
      button.textContent = '复制代码'
      header.append(button)
    }
    pre.before(block)
    block.append(header, pre)
  }

  for (const table of template.content.querySelectorAll('table')) {
    const scroll = document.createElement('div')
    scroll.className = 'markdown-body__table'
    scroll.tabIndex = 0
    scroll.setAttribute('role', 'region')
    scroll.setAttribute('aria-label', '表格')
    table.before(scroll)
    scroll.append(table)
  }

  return template.innerHTML
}
