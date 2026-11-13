import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRun, parseSnapshot, watchRun, type Snapshot } from './chat'

const snapshot: Snapshot = { run: { id: 'run-1', conversation_id: 'conv-1', status: 'running', model: 'test', error_code: null, error_message: null }, message: { id: 'msg-1', conversation_id: 'conv-1', run_id: 'run-1', sequence: 2, role: 'assistant', content: '部分回答', status: 'streaming' } }

afterEach(() => vi.unstubAllGlobals())
describe('问答接口', () => {
  it('提交时携带幂等键，校验运行和消息归属', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshot), { status: 202 }))
    vi.stubGlobal('fetch', fetcher)
    expect(await createRun('conv-1', '问题', 'same-key')).toEqual(snapshot)
    expect(JSON.parse(fetcher.mock.calls[0]?.[1]?.body as string)).toEqual({ content: '问题', idempotency_key: 'same-key' })
    expect(() => parseSnapshot({ ...snapshot, message: { ...snapshot.message, conversation_id: 'other' } })).toThrow('数据格式')
    expect(() => parseSnapshot({ run: { ...snapshot.run, status: 'unknown' }, message: snapshot.message })).toThrow('数据格式')
    const restored = parseSnapshot({
      run: { ...snapshot.run, id: 'run-2', status: 'failed', error_message: '失败' },
      message: { ...snapshot.message, run_id: 'run-1', content: '原来的回答', status: 'completed' },
    })
    expect(restored.message.id).toBe('msg-1')
    expect(restored.message.run_id).toBe('run-1')
    expect(() => parseSnapshot({ run: { ...snapshot.run, id: 'run-2', status: 'running' }, message: snapshot.message })).toThrow('数据格式')
  })
  it('快照覆盖与终态关闭连接，断线错误可展示', () => {
    class Source {
      static current: Source
      listeners = new Map<string, EventListener>()
      onerror: (() => void) | null = null
      close = vi.fn()
      constructor() { Source.current = this }
      addEventListener(name: string, listener: EventListener): void { this.listeners.set(name, listener) }
    }
    vi.stubGlobal('EventSource', Source)
    const receive = vi.fn()
    const connection = vi.fn()
    const dispose = watchRun('run-1', receive, connection)
    const source = Source.current
    source.onerror?.()
    expect(connection).toHaveBeenCalledWith(expect.stringContaining('重连'))
    source.listeners.get('snapshot')?.(new MessageEvent('snapshot', { data: JSON.stringify(snapshot) }))
    expect(receive).toHaveBeenCalledWith(snapshot)
    const completed = { ...snapshot, run: { ...snapshot.run, status: 'completed' }, message: { ...snapshot.message, content: '完整回答', status: 'completed' } }
    source.listeners.get('completed')?.(new MessageEvent('completed', { data: JSON.stringify(completed) }))
    expect(source.close).toHaveBeenCalled()
    const expired = vi.fn()
    window.addEventListener('auth-expired', expired)
    source.listeners.get('access_revoked')?.(new Event('access_revoked'))
    expect(expired).toHaveBeenCalledTimes(1)
    window.removeEventListener('auth-expired', expired)
    dispose()
    source.listeners.get('snapshot')?.(new MessageEvent('snapshot', { data: JSON.stringify(snapshot) }))
    expect(receive).toHaveBeenCalledTimes(2)
  })
})
