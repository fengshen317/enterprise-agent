import type { RetrievalTestChunk } from '../../api/knowledge'

export interface EvaluationCase { query: string; document: string; evidence: string }
export interface EvaluationSuite { name: string; version: string; top_k: number; threshold: number; cases: EvaluationCase[] }
export interface CaseResult extends EvaluationCase { rank: number | null; returned: number; effective_top_k: number }

export const maxSuiteBytes = 128 * 1024
const maxCases = 30
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object'
}
function boundedText(value: unknown, max: number): value is string {
  return typeof value === 'string' && Boolean(value.trim()) && value.length <= max
}

export function parseSuite(text: string): EvaluationSuite {
  let manifest: unknown
  try { manifest = JSON.parse(text.replace(/^\uFEFF/, '')) }
  catch { throw new Error('题集须为 UTF-8 JSON 文件') }
  if (!isRecord(manifest) || !boundedText(manifest.name, 100) || !boundedText(manifest.version, 64)
    || typeof manifest.top_k !== 'number' || !Number.isInteger(manifest.top_k) || manifest.top_k < 1 || manifest.top_k > 50
    || typeof manifest.threshold !== 'number' || manifest.threshold < 0 || manifest.threshold > 1
    || !Array.isArray(manifest.cases) || !manifest.cases.length || manifest.cases.length > maxCases) {
    throw new Error('题集须含名称、版本、1–50 的 top_k、0–1 的 threshold，以及 1–30 道题')
  }
  const seen = new Set<string>()
  const cases = manifest.cases.map((item: unknown, index: number) => {
    if (!isRecord(item) || !boundedText(item.query, 2000) || !boundedText(item.document, 255) || !boundedText(item.evidence, 500)) {
      throw new Error('第 ' + (index + 1) + ' 题缺少有效的问题、文档名或预期原文')
    }
    const query = item.query.trim()
    if (seen.has(query)) throw new Error('题集中有重复问题：' + query)
    seen.add(query)
    return { query, document: item.document.trim(), evidence: item.evidence.trim() }
  })
  return { name: manifest.name, version: manifest.version, top_k: manifest.top_k, threshold: manifest.threshold, cases }
}

function normalized(value: string): string {
  return value.normalize('NFKC').replace(/\s+/gu, '').toLowerCase()
}

export function scoreCase(item: EvaluationCase, chunks: RetrievalTestChunk[], limit: number): CaseResult {
  const ranked = chunks.slice(0, limit)
  const evidence = normalized(item.evidence)
  const index = ranked.findIndex(chunk => chunk.document_name === item.document && normalized(chunk.content).includes(evidence))
  return { ...item, rank: index === -1 ? null : index + 1, returned: ranked.length, effective_top_k: limit }
}

export function metrics(results: CaseResult[]): { hitRate: number; mrr: number } {
  const hits = results.filter(result => result.rank !== null)
  if (!results.length) return { hitRate: 0, mrr: 0 }
  return { hitRate: hits.length / results.length, mrr: hits.reduce((sum, item) => sum + 1 / item.rank!, 0) / results.length }
}
