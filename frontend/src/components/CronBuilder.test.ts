import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import CronBuilder from './CronBuilder.vue'

function emittedValue(wrapper: ReturnType<typeof mount>): string {
  const values = wrapper.emitted('update:modelValue')
  return String(values?.[values.length - 1]?.[0] ?? '')
}

function tab(wrapper: ReturnType<typeof mount>, label: string) {
  const found = wrapper.findAll('[role="tab"]').find(item => item.text() === label)
  if (!found) throw new Error('未找到时间模式：' + label)
  return found
}

it('解析每天任务并显示当前时间', () => {
  const wrapper = mount(CronBuilder, { props: { modelValue: '0 9 * * *' } })
  expect(tab(wrapper, '每天').attributes('aria-selected')).toBe('true')
  expect((wrapper.get('input[name="cronTime"]').element as HTMLInputElement).value).toBe('09:00')
  expect(wrapper.text()).toContain('0 9 * * *')
  wrapper.unmount()
})

it('修改时间后生成新的五段 Cron', async () => {
  const wrapper = mount(CronBuilder, { props: { modelValue: '0 9 * * *' } })
  await wrapper.get('input[name="cronTime"]').setValue('10:30')
  expect(emittedValue(wrapper)).toBe('30 10 * * *')
  wrapper.unmount()
})

it('每周可以多选星期', async () => {
  const wrapper = mount(CronBuilder, { props: { modelValue: '0 9 * * *' } })
  await tab(wrapper, '每周').trigger('click')
  expect(emittedValue(wrapper)).toBe('0 9 * * 1')
  const checks = wrapper.findAll('input[type="checkbox"]')
  await checks[2]!.setValue(true)
  expect(emittedValue(wrapper)).toBe('0 9 * * 1,3')
  wrapper.unmount()
})

it('固定间隔支持分钟和小时', async () => {
  const wrapper = mount(CronBuilder, { props: { modelValue: '0 9 * * *' } })
  await tab(wrapper, '固定间隔').trigger('click')
  expect(emittedValue(wrapper)).toBe('*/30 * * * *')
  await wrapper.get('input[name="cronInterval"]').setValue('15')
  expect(emittedValue(wrapper)).toBe('*/15 * * * *')
  await wrapper.get('select[name="cronIntervalUnit"]').setValue('hour')
  await wrapper.get('input[name="cronInterval"]').setValue('2')
  expect(emittedValue(wrapper)).toBe('0 */2 * * *')
  wrapper.unmount()
})

it('每月可以选择具体日期', async () => {
  const wrapper = mount(CronBuilder, { props: { modelValue: '0 9 * * *' } })
  await tab(wrapper, '每月').trigger('click')
  await wrapper.get('select[name="cronMonthDay"]').setValue('15')
  expect(emittedValue(wrapper)).toBe('0 9 15 * *')
  wrapper.unmount()
})

it('每小时任务也能回填到固定间隔', () => {
  const wrapper = mount(CronBuilder, { props: { modelValue: '0 * * * *' } })
  expect(tab(wrapper, '固定间隔').attributes('aria-selected')).toBe('true')
  expect((wrapper.get('input[name="cronInterval"]').element as HTMLInputElement).value).toBe('1')
  expect((wrapper.get('select[name="cronIntervalUnit"]').element as HTMLSelectElement).value).toBe('hour')
  wrapper.unmount()
})

it('无法归类的表达式回退到高级 Cron 并保持原文', () => {
  const wrapper = mount(CronBuilder, { props: { modelValue: '0 9 * * 1-5' } })
  expect(tab(wrapper, '高级 Cron').attributes('aria-selected')).toBe('true')
  expect((wrapper.get('input[name="cronExpression"]').element as HTMLInputElement).value).toBe('0 9 * * 1-5')
  wrapper.unmount()
})

it('编辑已有每月任务时回填并保留星期', () => {
  const monthly = mount(CronBuilder, { props: { modelValue: '0 9 1 * *' } })
  expect(tab(monthly, '每月').attributes('aria-selected')).toBe('true')
  expect((monthly.get('select[name="cronMonthDay"]').element as HTMLSelectElement).value).toBe('1')
  monthly.unmount()

  const weekly = mount(CronBuilder, { props: { modelValue: '0 9 * * 0' } })
  expect(tab(weekly, '每周').attributes('aria-selected')).toBe('true')
  expect((weekly.findAll('input[type="checkbox"]')[6]!.element as HTMLInputElement).checked).toBe(true)
  weekly.unmount()
})
