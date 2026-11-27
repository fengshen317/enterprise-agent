import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../../api/connectors'
import RssSourcePicker from './RssSourcePicker.vue'

vi.mock('../../api/connectors', () => ({ listRssSources: vi.fn() }))

const openai = 'https://openai.com/news/rss.xml'
const google = 'https://blog.google/technology/ai/rss/'
const custom = 'https://example.com/feed'

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.listRssSources).mockResolvedValue([
    { name: 'OpenAI 官方动态', category: '官方动态', url: openai },
    { name: 'Google AI 官方博客', category: '官方动态', url: google },
  ])
})

it('识别已有预置订阅，取消勾选保留自定义源，加载不改写配置', async () => {
  const wrapper = mount(RssSourcePicker, { props: { modelValue: JSON.stringify([openai, custom]) } })
  await flushPromises()
  expect(wrapper.get<HTMLInputElement>(`input[value="${openai}"]`).element.checked).toBe(true)
  expect(wrapper.get('textarea').element.value).toBe(custom)
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  await wrapper.get(`input[value="${openai}"]`).setValue(false)
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([JSON.stringify([custom])])
  wrapper.unmount()
})

it('自定义源多行输入保留换行，和预置源合并后去重', async () => {
  const wrapper = mount(RssSourcePicker, { props: { modelValue: '[]' } })
  await flushPromises()
  await wrapper.get('textarea').setValue(custom + '\n')
  const emitted = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as string
  await wrapper.setProps({ modelValue: emitted })
  expect(wrapper.get('textarea').element.value).toBe(custom + '\n')
  await wrapper.get('textarea').setValue(custom + '\n' + openai)
  expect(wrapper.get<HTMLInputElement>(`input[value="${openai}"]`).element.checked).toBe(true)
  await wrapper.get(`input[value="${openai}"]`).setValue(false)
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([JSON.stringify([custom])])
  wrapper.unmount()
})

it('达到十个源后限制新增选择，已有选择仍能取消', async () => {
  const urls = [openai, ...Array.from({ length: 9 }, (_, i) => `https://example.com/feed/${i}`)]
  const wrapper = mount(RssSourcePicker, { props: { modelValue: JSON.stringify(urls) } })
  await flushPromises()
  expect(wrapper.get(`input[value="${google}"]`).attributes('disabled')).toBeDefined()
  expect(wrapper.get(`input[value="${openai}"]`).attributes('disabled')).toBeUndefined()
  await wrapper.get(`input[value="${openai}"]`).setValue(false)
  expect(wrapper.get(`input[value="${google}"]`).attributes('disabled')).toBeUndefined()
  wrapper.unmount()
})

it('目录加载失败时保留已有配置，重试后识别预置源', async () => {
  vi.mocked(api.listRssSources).mockRejectedValueOnce(new Error('network'))
  const wrapper = mount(RssSourcePicker, { props: { modelValue: JSON.stringify([openai, custom]), disabled: true } })
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
  expect(wrapper.get('textarea').element.value).toBe(openai + '\n' + custom)
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.get<HTMLInputElement>(`input[value="${openai}"]`).element.checked).toBe(true)
  expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
  wrapper.unmount()
})

it('异常历史配置保留原文，修正后恢复勾选界面', async () => {
  const wrapper = mount(RssSourcePicker, { props: { modelValue: 'bad-json' } })
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('bad-json')
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  await wrapper.setProps({ modelValue: JSON.stringify([openai]) })
  expect(wrapper.get<HTMLInputElement>(`input[value="${openai}"]`).element.checked).toBe(true)
  wrapper.unmount()
})

it('需要适配的社交信源显示说明，并保持不可勾选', async () => {
  vi.mocked(api.listRssSources).mockResolvedValue([
    { name: 'X / Twitter', category: '社交平台', url: '', description: '待配置：需要 RSSHub 生成账号订阅' },
    { name: 'Anthropic · Claude Code', category: '厂商官方项目', url: openai, description: '项目版本发布' },
  ])
  const wrapper = mount(RssSourcePicker, { props: { modelValue: '[]' } })
  await flushPromises()
  expect(wrapper.text()).toContain('需要 RSSHub')
  expect(wrapper.get('input[value=""]').attributes('disabled')).toBeDefined()
  await wrapper.get(`input[value="${openai}"]`).setValue(true)
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([JSON.stringify([openai])])
  wrapper.unmount()
})
