<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { retrievalTest, type KnowledgeBase } from '../../api/knowledge'
import { maxSuiteBytes, metrics, parseSuite, scoreCase, type CaseResult, type EvaluationSuite } from './retrievalEvaluation'

const props = defineProps<{ kb: KnowledgeBase }>()
const suite = ref<EvaluationSuite>()
const results = ref<CaseResult[]>([])
const busy = ref(false)
const reading = ref(false)
const error = ref('')
const startedAt = ref('')
const reportUrl = ref('')
const summary = computed(() => metrics(results.value))
const complete = computed(() => Boolean(suite.value && results.value.length === suite.value.cases.length))
let generation = 0

function releaseReport(): void {
  if (reportUrl.value) URL.revokeObjectURL(reportUrl.value)
  reportUrl.value = ''
}

async function importSuite(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || busy.value) return
  const token = ++generation
  suite.value = undefined
  results.value = []
  reading.value = true
  error.value = ''
  releaseReport()
  try {
    if (file.size > maxSuiteBytes) throw new Error('题集最多 128 KB')
    const text = await file.text()
    if (token === generation) suite.value = parseSuite(text)
  } catch (value) {
    if (token === generation) error.value = value instanceof Error ? value.message : '题集读取失败'
  } finally {
    if (token === generation) reading.value = false
  }
}

async function run(): Promise<void> {
  const manifest = suite.value
  if (!manifest || busy.value || reading.value) return
  const token = ++generation
  const kb = props.kb
  busy.value = true
  results.value = []
  error.value = ''
  releaseReport()
  startedAt.value = new Date().toISOString()
  try {
    for (const item of manifest.cases) {
      const result = await retrievalTest(kb.id, { query: item.query, top_k: manifest.top_k, threshold: manifest.threshold })
      if (generation !== token) return
      results.value.push(scoreCase(item, result.chunks, result.effective_top_k))
    }
    const report = {
      started_at: startedAt.value, finished_at: new Date().toISOString(),
      knowledge_base: { id: kb.id, name: kb.name, updated_at: kb.updated_at, embedding_model: kb.embedding_model,
        parser_id: kb.parser_id, parser_settings: kb.parser_settings, document_count: kb.document_count, chunk_count: kb.chunk_count },
      suite: manifest, metrics: summary.value, results: results.value,
    }
    reportUrl.value = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }))
  } catch (value) {
    if (token === generation) error.value = '第 ' + (results.value.length + 1) + ' 题未完成：' + (value instanceof Error ? value.message : '检索失败')
  } finally { if (token === generation) busy.value = false }
}

onBeforeUnmount(() => { generation++; releaseReport() })
</script>

<template>
  <section
    class="evaluation"
    aria-label="批量检索评测"
  >
    <h2>批量检索评测</h2>
    <p>导入固定题集，核对检索片段是否同时命中指定文档和预期原文。</p>
    <p>
      <a
        href="/knowledge-evaluation-sample.zip"
        download
      >下载样本资料与题集</a> · 先在测试知识库上传并解析样本文档，再导入包内的 JSON。
    </p>
    <label>评测题集
      <input
        type="file"
        accept=".json,application/json"
        :disabled="busy || reading"
        @change="importSuite"
      />
    </label>
    <template v-if="suite">
      <p>{{ suite.name }} · {{ suite.version }} · {{ suite.cases.length }} 题 · 请求前 {{ suite.top_k }} 条 · 阈值 {{ suite.threshold }}</p>
      <button
        type="button"
        class="evaluation__run"
        :disabled="busy"
        @click="run"
      >
        {{ busy ? '已检索 ' + results.length + '/' + suite.cases.length : '运行题集' }}
      </button>
    </template>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="complete && reportUrl"
      role="status"
    >
      命中率 {{ (summary.hitRate * 100).toFixed(1) }}% · 平均倒数排名（MRR）{{ summary.mrr.toFixed(3) }} ·
      <a
        :href="reportUrl"
        download="knowledge-evaluation.json"
      >下载结果</a>
    </p>
    <ol
      v-if="results.length"
      class="evaluation__results"
    >
      <li
        v-for="item in results"
        :key="item.query"
      >
        {{ item.query }} <strong>{{ item.rank === null ? '未命中' : '第 ' + item.rank + ' 位' }}</strong>
        <small>{{ item.document }} · 预期原文「{{ item.evidence }}」 · 返回 {{ item.returned }} 条，上限 {{ item.effective_top_k }} 条</small>
      </li>
    </ol>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
@use '../../styles/knowledge';
.evaluation {
  padding: 22px; margin: 18px 0; border: 1px solid $line; border-radius: 8px; background: $surface;
  h2 { font-size: 17px; font-weight: 500; }
  p, small { color: $muted; font-size: 12px; line-height: 1.7; }
  a { color: $green; }
  label { display: grid; gap: 6px; font-size: 13px; }
  &__run { padding: 8px 14px; border: 0; border-radius: 5px; background: $green; color: $on-green; }
  &__results { padding-left: 20px; }
  li { margin: 10px 0; font-size: 13px; overflow-wrap: anywhere; }
  small { display: block; }
}
@media (min-width: $knowledge-stack-width + 1px) {
  .evaluation {
    @include knowledge.desktop-surface;
    padding: 24px;
    h2 { font-size: 20px; font-weight: 600; margin-bottom: 6px; }
    p, small { color: $log-muted; font-size: 13px; }
  }
}
</style>
