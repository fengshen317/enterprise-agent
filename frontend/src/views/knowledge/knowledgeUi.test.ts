import { expect, it } from 'vitest'
import {
  PARSER_TEMPLATES,
  defaultParserForm,
  errorLabel,
  formatDelimiters,
  parseDelimiters,
  parserForm,
  progressLabel,
  settingsFromForm,
  statusLabel,
} from './knowledgeUi'
import type { ParserSettings } from '../../api/knowledge'

const settings: ParserSettings = {
  chunk_token_size: 256,
  delimiters: ['\n', '。'],
  overlapped_percent: 0.1,
  use_parent_child: false,
  parent_child_delimiter: '\n',
  pdf_parse_method: 'mineru',
  auto_keywords_top_n: 5,
  auto_questions_top_n: null,
}

it('分隔符文本与数组可双向转换', () => {
  const text = '\\n,。,；'
  expect(parseDelimiters(text)).toEqual(['\n', '。', '；'])
  expect(formatDelimiters(['\n', '。', '；'])).toBe(text)
})

it('解析表单转换空自动字段为 null', () => {
  const form = defaultParserForm()
  form.autoKeywordsTopN = ''
  form.autoQuestionsTopN = 3
  const result = settingsFromForm(form)
  expect(result.auto_keywords_top_n).toBeNull()
  expect(result.auto_questions_top_n).toBe(3)
  expect(result.pdf_parse_method).toBe('deepdoc')
})

it('从知识库配置回填解析表单', () => {
  const form = parserForm(settings, 'manual')
  expect(form.parserId).toBe('manual')
  expect(form.chunkTokenSize).toBe(256)
  expect(form.pdfParseMethod).toBe('mineru')
  expect(form.autoKeywordsTopN).toBe(5)
  expect(form.autoQuestionsTopN).toBe('')
})

it('预设模板都生成合法解析设置', () => {
  for (const item of PARSER_TEMPLATES) {
    const value = settingsFromForm(item.form)
    expect(value.chunk_token_size).toBeGreaterThanOrEqual(64)
    expect(value.chunk_token_size).toBeLessThanOrEqual(4096)
    expect(value.delimiters.length).toBeGreaterThan(0)
  }
})

it('进度同时兼容 0–1 与 0–100 刻度', () => {
  expect(progressLabel(0.5)).toBe('50%')
  expect(progressLabel(50)).toBe('50%')
  expect(progressLabel(1)).toBe('100%')
})

it('状态与错误码有中文映射', () => {
  expect(statusLabel('ready')).toBe('就绪')
  expect(statusLabel('parsing')).toBe('解析中')
  expect(errorLabel('RAGFLOW_PARSE_FAILED')).toBe('解析失败')
  expect(errorLabel(null)).toBe('')
})
