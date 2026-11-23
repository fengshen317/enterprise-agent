import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  listWeChatAccounts,
  pollWeChatBind,
  startWeChatBind,
  unbindWeChat,
  type WeChatBindSession,
} from '../../api/channels'
import WeChatChannelView from './WeChatChannelView.vue'

const qrcodeMocks = vi.hoisted(() => ({
  toDataURL: vi.fn<(text: string, options?: object) => Promise<string>>(),
}))

vi.mock('qrcode', () => qrcodeMocks)

vi.mock('../../api/channels', () => ({
  listWeChatAccounts: vi.fn(),
  pollWeChatBind: vi.fn(),
  startWeChatBind: vi.fn(),
  unbindWeChat: vi.fn(),
}))

const account = {
  id: 'account-1',
  workspace_id: 'ws-1',
  status: 'active',
  ilink_bot_id: 'bot-1',
  ilink_user_id: 'wx-user',
  paused_until: null,
  created_at: '2026-11-13T00:00:00Z',
  window_open: false,
  window_until: null,
  window_note: '推送已暂停',
  skipped_pushes: [{ created_at: '2026-11-13T00:00:00Z', reason: 'outside_window', excerpt: '晨报正文' }],
}

describe('微信绑定页', () => {
  beforeEach(() => {
    vi.mocked(listWeChatAccounts).mockReset()
    vi.mocked(pollWeChatBind).mockReset()
    vi.mocked(startWeChatBind).mockReset()
    vi.mocked(unbindWeChat).mockReset()
    qrcodeMocks.toDataURL.mockReset()
    qrcodeMocks.toDataURL.mockResolvedValue('data:image/png;base64,generated-qr')
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('开始绑定后把微信登录链接编码为二维码', async () => {
    const loginUrl = 'https://liteapp.weixin.qq.com/q/test?qrcode=session-1'
    vi.mocked(listWeChatAccounts).mockResolvedValue([])
    vi.mocked(startWeChatBind).mockResolvedValue({
      session_id: 'session-1',
      status: 'pending',
      qrcode_img_content: loginUrl,
      account_id: null,
      ilink_user_id: '',
      message: '等待扫码',
    })
    vi.mocked(pollWeChatBind).mockResolvedValue({
      session_id: 'session-1',
      status: 'wait',
      qrcode_img_content: loginUrl,
      account_id: null,
      ilink_user_id: '',
      message: '等待扫码',
    })
    const wrapper = mount(WeChatChannelView)
    await flushPromises()
    await wrapper.get('button.wechat__bind').trigger('click')
    await flushPromises()
    expect(qrcodeMocks.toDataURL).toHaveBeenCalledWith(loginUrl, expect.objectContaining({ width: 220 }))
    expect(wrapper.get('img').attributes('src')).toBe('data:image/png;base64,generated-qr')
    expect(wrapper.get('a').attributes('href')).toBe(loginUrl)
    expect(wrapper.text()).toContain('等待扫码')
    wrapper.unmount()
  })

  it('上一次轮询完成后才安排下一次', async () => {
    vi.useFakeTimers()
    let resolvePoll: ((value: WeChatBindSession) => void) | undefined
    const pendingPoll = new Promise<WeChatBindSession>((resolve) => { resolvePoll = resolve })
    const bindSession: WeChatBindSession = {
      session_id: 'session-1',
      status: 'wait',
      qrcode_img_content: 'https://liteapp.weixin.qq.com/q/test',
      account_id: null,
      ilink_user_id: '',
      message: '等待扫码',
    }
    vi.mocked(listWeChatAccounts).mockResolvedValue([])
    vi.mocked(startWeChatBind).mockResolvedValue({ ...bindSession, status: 'pending' })
    vi.mocked(pollWeChatBind)
      .mockResolvedValueOnce(bindSession)
      .mockReturnValueOnce(pendingPoll)
      .mockResolvedValue(bindSession)
    const wrapper = mount(WeChatChannelView)
    await flushPromises()
    await wrapper.get('button.wechat__bind').trigger('click')
    await flushPromises()

    expect(pollWeChatBind).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2000)
    expect(pollWeChatBind).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(6000)
    expect(pollWeChatBind).toHaveBeenCalledTimes(2)

    resolvePoll?.(bindSession)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1999)
    expect(pollWeChatBind).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(pollWeChatBind).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('可以解除已有绑定', async () => {
    vi.mocked(listWeChatAccounts).mockResolvedValue([account])
    const wrapper = mount(WeChatChannelView)
    await flushPromises()
    expect(wrapper.text()).toContain('微信用户 wx-user')
    expect(wrapper.text()).toContain('推送已暂停')
    expect(wrapper.text()).toContain('未推送（已超过 24 小时）：晨报正文')
    await wrapper.get('.wechat__account button').trigger('click')
    await flushPromises()
    expect(unbindWeChat).toHaveBeenCalledWith('account-1')
    wrapper.unmount()
  })
})
