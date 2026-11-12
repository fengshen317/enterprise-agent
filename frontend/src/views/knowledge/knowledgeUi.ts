import type { ParserSettings } from '../../api/knowledge'

export type PdfParseMethod = '' | 'deepdoc' | 'mineru'

export interface ParserForm {
  parserId: string
  chunkTokenSize: number
  delimitersText: string
  overlappedPercent: number
  pdfParseMethod: PdfParseMethod
  autoKeywordsTopN: number | ''
  autoQuestionsTopN: number | ''
}

export interface ParserTemplate {
  key: string
  label: string
  description: string
  parserId: string
  form: ParserForm
}

export function defaultParserForm(): ParserForm {
  return {
    parserId: 'general',
    chunkTokenSize: 512,
    delimitersText: '\\n,。,；,！,？',
    overlappedPercent: 0.1,
    pdfParseMethod: 'deepdoc',
    autoKeywordsTopN: '',
    autoQuestionsTopN: '',
  }
}

export function parserForm(settings: ParserSettings, parserId = 'general'): ParserForm {
  return {
    parserId: parserId || 'general',
    chunkTokenSize: settings.chunk_token_size,
    delimitersText: formatDelimiters(settings.delimiters),
    overlappedPercent: settings.overlapped_percent,
    pdfParseMethod: settings.pdf_parse_method ?? '',
    autoKeywordsTopN: settings.auto_keywords_top_n ?? '',
    autoQuestionsTopN: settings.auto_questions_top_n ?? '',
  }
}

export function parseDelimiters(text: string): string[] {
  const values = text.split(',')
    .map(item => item.trim())
    .map(item => item === '\\n' ? '\n' : item === '\\t' ? '\t' : item)
    .filter(item => item.length > 0)
  return values.length ? values : ['\n']
}

export function formatDelimiters(values: string[]): string {
  return values.map(item => item === '\n' ? '\\n' : item === '\t' ? '\\t' : item).join(',')
}

export function settingsFromForm(form: ParserForm): ParserSettings {
  return {
    chunk_token_size: Number(form.chunkTokenSize) || 512,
    delimiters: parseDelimiters(form.delimitersText),
    overlapped_percent: Math.max(0, Math.min(0.5, Number(form.overlappedPercent) || 0)),
    use_parent_child: false,
    parent_child_delimiter: '\n',
    pdf_parse_method: form.pdfParseMethod === '' ? null : form.pdfParseMethod,
    auto_keywords_top_n: form.autoKeywordsTopN === '' ? null : Number(form.autoKeywordsTopN),
    auto_questions_top_n: form.autoQuestionsTopN === '' ? null : Number(form.autoQuestionsTopN),
  }
}

export const PARSER_TEMPLATES: ParserTemplate[] = [
  {
    key: 'general',
    label: '通用文本',
    description: '制度、说明、报告等普通文档',
    parserId: 'general',
    form: {
      parserId: 'general',
      chunkTokenSize: 512,
      delimitersText: '\\n,。,；,！,？',
      overlappedPercent: 0.1,
      pdfParseMethod: 'deepdoc',
      autoKeywordsTopN: '',
      autoQuestionsTopN: '',
    },
  },
  {
    key: 'manual',
    label: '制度/操作手册',
    description: '手册、规范、长文档；使用更强的 PDF 解析',
    parserId: 'manual',
    form: {
      parserId: 'manual',
      chunkTokenSize: 768,
      delimitersText: '\\n,。,；',
      overlappedPercent: 0.1,
      pdfParseMethod: 'mineru',
      autoKeywordsTopN: 5,
      autoQuestionsTopN: '',
    },
  },
  {
    key: 'faq',
    label: 'FAQ / 问答',
    description: '问答对、短条目；自动生成问题',
    parserId: 'general',
    form: {
      parserId: 'general',
      chunkTokenSize: 256,
      delimitersText: '\\n',
      overlappedPercent: 0,
      pdfParseMethod: 'deepdoc',
      autoKeywordsTopN: '',
      autoQuestionsTopN: 5,
    },
  },
]

const STATUS_LABELS: Record<string, string> = {
  creating: '创建中',
  ready: '就绪',
  failed: '失败',
  configuring: '配置中',
  deleting: '删除中',
  delete_failed: '删除失败',
  uploading: '上传中',
  uploaded: '已上传',
  parsing: '解析中',
}

const ERROR_LABELS: Record<string, string> = {
  RAGFLOW_NOT_CONFIGURED: 'RAGFlow 未配置',
  RAGFLOW_UNAVAILABLE: '知识库服务不可用',
  RAGFLOW_TIMEOUT: '知识库服务超时',
  RAGFLOW_REJECTED: '知识库服务拒绝了操作',
  RAGFLOW_PARSE_FAILED: '解析失败',
  RAGFLOW_PARSE_CANCELLED: '解析已取消',
  KNOWLEDGE_CREATE_FAILED: '创建失败',
  KNOWLEDGE_UPLOAD_FAILED: '上传失败',
  KNOWLEDGE_FILE_TYPE: '不支持的文件类型',
  KNOWLEDGE_FILE_TOO_LARGE: '文件超过大小限制',
  KNOWLEDGE_NOT_READY: '知识库尚未就绪',
  DOCUMENT_NOT_READY: '文档尚未就绪',
  KNOWLEDGE_ACCESS_DENIED: '没有可访问的知识库',
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

export function errorLabel(code: string | null): string {
  return code ? ERROR_LABELS[code] ?? code : ''
}

export function timeLabel(value: string): string {
  return new Date(value).toLocaleString('zh-CN')
}

export function progressLabel(progress: number): string {
  const value = progress > 1 ? progress : progress * 100
  return Math.max(0, Math.min(100, Math.round(value))) + '%'
}

export function formatSize(size: number): string {
  if (size >= 1024 * 1024) return (size / 1024 / 1024).toFixed(1) + ' MB'
  return (size / 1024).toFixed(1) + ' KB'
}

export function parserIdLabel(parserId: string): string {
  return parserId === 'manual' ? '操作手册' : '通用'
}
