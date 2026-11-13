import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { listWeChatAccounts, pollWeChatBind, startWeChatBind, unbindWeChat } from '../../api/channels'
import WeChatChannelView from './WeChatChannelView.vue'

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
}

describe('微信绑定页', () => {
  beforeEach(() => {
    vi.mocked(listWeChatAccounts).mockReset()
    vi.mocked(pollWeChatBind).mockReset()
    vi.mocked(startWeChatBind).mockReset()
    vi.mocked(unbindWeChat).mockReset()
  })

  it('开始绑定后展示二维码', async () => {
    vi.mocked(listWeChatAccounts).mockResolvedValue([])
    vi.mocked(startWeChatBind).mockResolvedValue({
      session_id: 'session-1',
      status: 'pending',
      qrcode_img_content: 'https://example.test/qr.png',
      account_id: null,
      ilink_user_id: '',
      message: '等待扫码',
    })
    vi.mocked(pollWeChatBind).mockResolvedValue({
      session_id: 'session-1',
      status: 'wait',
      qrcode_img_content: 'https://example.test/qr.png',
      account_id: null,
      ilink_user_id: '',
      message: '等待扫码',
    })
    const wrapper = mount(WeChatChannelView)
    await flushPromises()
    await wrapper.get('button.wechat__bind').trigger('click')
    await flushPromises()
    expect(wrapper.get('img').attributes('src')).toBe('https://example.test/qr.png')
    expect(wrapper.text()).toContain('等待扫码')
    wrapper.unmount()
  })

  it('可以解除已有绑定', async () => {
    vi.mocked(listWeChatAccounts).mockResolvedValue([account])
    const wrapper = mount(WeChatChannelView)
    await flushPromises()
    expect(wrapper.text()).toContain('微信用户 wx-user')
    await wrapper.get('.wechat__account button').trigger('click')
    await flushPromises()
    expect(unbindWeChat).toHaveBeenCalledWith('account-1')
    wrapper.unmount()
  })
})
