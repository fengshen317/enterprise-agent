<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import { connectorTime, getYangJiBaoHoldings, type YangJiBaoPortfolio } from '../../api/connectors'

const portfolio = ref<YangJiBaoPortfolio | null>(null)
const loading = ref(false)
const error = ref('')
const expandedCode = ref<string | null>(null)
const amountFormatter = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const unitFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 })
const changeFormatter = new Intl.NumberFormat('zh-CN', {
  style: 'percent', minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero',
})
let active = true
onUnmounted(() => { active = false })

async function refreshHoldings(): Promise<void> {
  if (loading.value) return
  loading.value = true
  portfolio.value = null
  error.value = ''
  try {
    const result = await getYangJiBaoHoldings()
    if (active) portfolio.value = result
  } catch (value) {
    if (active) error.value = value instanceof Error ? value.message : '持仓同步失败，请重试'
  } finally {
    if (active) loading.value = false
  }
}

function formatChange(value: number | null): string {
  return value === null ? '暂无' : changeFormatter.format(value / 100)
}

function formatMetric(value: number | null | undefined, formatter: Intl.NumberFormat): string {
  return value == null ? '暂无' : formatter.format(value)
}
</script>

<template>
  <section
    class="yangjibao-holdings"
    :aria-busy="loading"
  >
    <header class="yangjibao-holdings__header">
      <h3>我的持仓</h3>
      <button
        type="button"
        :disabled="loading"
        @click="refreshHoldings"
      >
        {{ loading ? '同步中…' : portfolio ? '刷新持仓' : '查看持仓' }}
      </button>
    </header>
    <p
      v-if="error"
      role="alert"
      class="yangjibao-holdings__error"
    >
      {{ error }}
    </p>
    <template v-if="portfolio">
      <p class="yangjibao-holdings__meta">
        同步于 {{ connectorTime(portfolio.synced_at) }} · 共 {{ portfolio.fund_count }} 只
      </p>
      <p v-if="!portfolio.funds.length">
        暂无持仓
      </p>
      <div
        v-else
        class="yangjibao-holdings__scroll"
      >
        <table class="yangjibao-holdings__table">
          <thead>
            <tr>
              <th scope="col">
                基金
              </th>
              <th scope="col">
                持仓金额
              </th>
              <th scope="col">
                盘中估算涨跌幅
              </th>
              <th scope="col">
                披露净值涨跌幅
              </th>
            </tr>
          </thead>
          <tbody
            v-for="fund in portfolio.funds"
            :key="fund.code"
          >
            <tr>
              <th scope="row">
                <button
                  type="button"
                  class="yangjibao-holdings__expand"
                  :aria-expanded="expandedCode === fund.code"
                  :aria-label="`${expandedCode === fund.code ? '收起' : '查看'} ${fund.name || fund.code} 的持仓详情`"
                  @click="expandedCode = expandedCode === fund.code ? null : fund.code"
                >
                  {{ fund.name || fund.code }}
                  <small>{{ fund.name ? `${fund.code} · ` : '' }}{{ expandedCode === fund.code ? '收起' : '详情' }}</small>
                </button>
              </th>
              <td>
                {{ formatMetric(fund.holding_amount, amountFormatter) }}
              </td>
              <td>
                {{ formatChange(fund.estimated_change_percent) }}
                <small v-if="fund.estimated_change_percent !== null">{{ fund.quote_time || '时间未知' }}</small>
              </td>
              <td>
                {{ formatChange(fund.latest_nav_change_percent) }}
                <small v-if="fund.latest_nav_change_percent !== null">{{ fund.nav_date || '日期未知' }}</small>
              </td>
            </tr>
            <tr v-if="expandedCode === fund.code">
              <td
                colspan="4"
                class="yangjibao-holdings__expanded"
              >
                <dl class="yangjibao-holdings__metrics">
                  <div>
                    <dt>持有份额</dt>
                    <dd>{{ formatMetric(fund.share, unitFormatter) }}</dd>
                  </div>
                  <div>
                    <dt>每份成本（元）</dt>
                    <dd>{{ formatMetric(fund.cost_nav, unitFormatter) }}</dd>
                  </div>
                  <div>
                    <dt>持仓成本</dt>
                    <dd>{{ formatMetric(fund.cost_amount, amountFormatter) }}</dd>
                  </div>
                  <div>
                    <dt>盘中估算市值</dt>
                    <dd>{{ formatMetric(fund.estimated_amount, amountFormatter) }}</dd>
                  </div>
                  <div>
                    <dt>按最新净值计算的市值</dt>
                    <dd>{{ formatMetric(fund.latest_nav_amount, amountFormatter) }}</dd>
                  </div>
                  <div>
                    <dt>参考单日收益</dt>
                    <dd>{{ formatMetric(fund.estimated_today_profit_amount, amountFormatter) }}</dd>
                  </div>
                </dl>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.yangjibao-holdings {
  padding: 1rem 1.5rem;

  &__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;

    h3 { margin: 0; font-size: 0.95rem; }
    button {
      padding: 0.35rem 0.7rem;
      border: 1px solid $line;
      border-radius: 6px;
      background: $paper;
      color: $ink;
      cursor: pointer;

      &:disabled { opacity: 0.6; cursor: wait; }
    }
  }

  &__meta, small { color: $muted; font-size: 0.75rem; }
  &__error { font-size: 0.85rem; color: #c0392b; }
  &__scroll { overflow-x: auto; }
  &__expand {
    padding: 0;
    border: none;
    background: transparent;
    color: $ink;
    text-align: left;
    font: inherit;
    font-weight: 600;
    cursor: pointer;

    &:hover { text-decoration: underline; }
    small { color: $green; }
  }

  &__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;

    th, td {
      padding: 0.5rem 0.3rem;
      border-bottom: 1px solid $line;
      vertical-align: top;
      text-align: right;
    }

    th:first-child { text-align: left; width: 28%; }
    td { white-space: nowrap; }
    small { display: block; margin-top: 0.2rem; font-weight: normal; }

    .yangjibao-holdings__expanded {
      padding: 0.75rem;
      background: $paper;
      text-align: left;
      white-space: normal;
    }
  }

  &__metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.65rem 0.8rem;
    margin: 0;

    dt { color: $muted; }
    dd { margin: 0.2rem 0 0; font-weight: 600; }
  }
}
</style>
