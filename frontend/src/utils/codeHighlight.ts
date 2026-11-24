import highlighter from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'

const MAX_HIGHLIGHT_LENGTH = 12_000
const GRAMMARS = { bash, css, javascript, json, python, sql, typescript, xml, yaml }

// 只加载常用语法，避免对未标注语言的内容和流式片段做语言猜测。
for (const [name, grammar] of Object.entries(GRAMMARS)) highlighter.registerLanguage(name, grammar)

export function highlightCode(source: string, language: string): string | undefined {
  if (source.length > MAX_HIGHLIGHT_LENGTH || !highlighter.getLanguage(language)) return
  return highlighter.highlight(source, { language, ignoreIllegals: true }).value
}
