import { beforeEach, describe, expect, it, vi } from 'vitest'
import { requestJson } from './client'
import { pollWeChatBind, startWeChatBind } from './channels'

vi.mock('./client', () => ({
  ApiError: class ApiError extends Error {
    constructor(message: string, public readonly status: number) {
      super(message)
    }
  },
  requestJson: vi.fn(),
}))

const bindSession = {
  session_id: 'session-1',
  status: 'wait',
  qrcode_img_content: 'https://liteapp.weixin.qq.com/q/test',
  account_id: null,
  ilink_user_id: '',
  message: '等待扫码',
}

describe('微信渠道接口', () => {
  beforeEach(() => {
    vi.mocked(requestJson).mockReset()
    vi.mocked(requestJson).mockResolvedValue(bindSession)
  })

  it('取码和轮询超时覆盖 iLink 最长等待时间', async () => {
    await startWeChatBind()
    await pollWeChatBind('session-1')

    expect(requestJson).toHaveBeenNthCalledWith(
      1,
      '/api/channels/wechat/bind',
      { method: 'POST' },
      20000,
    )
    expect(requestJson).toHaveBeenNthCalledWith(
      2,
      '/api/channels/wechat/bind/session-1',
      {},
      15000,
    )
  })
})
