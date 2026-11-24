import { BarChart, LineChart, type BarSeriesOption, type LineSeriesOption } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent, type GridComponentOption, type LegendComponentOption, type TooltipComponentOption } from 'echarts/components'
import { init, use, type ComposeOption, type EChartsType } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import type { ChatReport, ReportChartSpec } from '../api/chatReports'

use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

type ReportChartOption = ComposeOption<BarSeriesOption | LineSeriesOption | GridComponentOption
  | LegendComponentOption | TooltipComponentOption>

export function buildReportChartOption(report: ChatReport, chart: ReportChartSpec): ReportChartOption {
  const xIndex = report.columns.indexOf(chart.x)
  const series = chart.y.map((column): BarSeriesOption | LineSeriesOption => {
    const index = report.columns.indexOf(column)
    const data = report.rows.map(row => typeof row[index] === 'number' ? row[index] : null)
    return chart.type === 'bar'
      ? { name: column, type: 'bar', data, barMaxWidth: 38 }
      : { name: column, type: 'line', data, showSymbol: data.length <= 20 }
  })
  return {
    animation: false,
    color: ['#17664f', '#3d68a0', '#a65b35', '#71538d', '#217785'],
    textStyle: { color: '#203b33' },
    // Canvas 提示将表格中的内容作为文本绘制，避免列名或单元格进入 HTML。
    tooltip: { trigger: 'axis', renderMode: 'richText', confine: true },
    grid: { left: 42, right: 16, top: chart.unit ? 53 : 39, bottom: 42 },
    legend: { type: 'scroll', show: series.length > 1, top: 4, left: 4, right: 4 },
    xAxis: {
      type: 'category',
      data: report.rows.map(row => row[xIndex] == null ? '—' : String(row[xIndex])),
      axisLabel: { hideOverlap: true, overflow: 'truncate', width: 84 },
    },
    yAxis: {
      type: 'value',
      name: chart.unit ?? undefined,
      axisLabel: { hideOverlap: true },
    },
    series,
  }
}

export function renderReportChart(host: HTMLElement, report: ChatReport, chart: ReportChartSpec): EChartsType {
  const instance = init(host)
  try {
    instance.setOption(buildReportChartOption(report, chart))
    return instance
  } catch (error) {
    instance.dispose()
    throw error
  }
}
