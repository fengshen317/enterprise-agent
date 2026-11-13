import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { getHealth } from '../api/health'
import SystemStatusView from './SystemStatusView.vue'

vi.mock('../api/health', () => ({ getHealth: vi.fn() }))

describe('系统状态展示', () => {
  it('数据库失败时仍展示 API 存活，并允许重试恢复', async () => {
    vi.mocked(getHealth).mockImplementation(async (kind) => {
      if (kind === 'ready') throw new ApiError('数据库未就绪', 503, 'request-test')
      return { status: 'ok', service: 'api', version: '0.1.0' }
    })
    const wrapper = mount(SystemStatusView)
    await flushPromises()
    expect(wrapper.findAll('.status-card')[0]?.text()).toContain('正常')
    expect(wrapper.findAll('.status-card')[1]?.text()).toContain('数据库未就绪')
    expect(wrapper.text()).toContain('request-test')
    vi.mocked(getHealth).mockImplementation(async kind => ({ status: kind === 'live' ? 'ok' : 'ready', service: 'api', version: '0.1.0' }))
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.status-card')[1]?.text()).toContain('基础迁移已完成')
    wrapper.unmount()
  })
})
