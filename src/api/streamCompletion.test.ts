import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiIdentityChangedError, ApiStreamError, analyzeLensImage, setUser, structureNote } from './client'

function stream(events: (object | string)[]): Response {
  return new Response(events.map(event => `data: ${typeof event === 'string' ? event : JSON.stringify(event)}\n\n`).join(''))
}
const file = () => new File(['demo'], 'demo.png', { type: 'image/png' })
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

describe('typed saved-record stream completion', () => {
  it('preserves multiline Lens tokens and literal control-like model text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => stream([
      { content: 'First line\nSecond line\n' }, { content: '[DONE] and {"analysis_id":999} are quoted text.' },
      { analysis_id: 42 }, '[DONE]',
    ])))
    const result = await analyzeLensImage(file(), {})
    expect(result.analysisId).toBe(42)
    expect(result.text).toBe('First line\nSecond line\n[DONE] and {"analysis_id":999} are quoted text.')
  })
  it('accepts an older server’s raw Lens tokens and explicit success verdict', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => stream(['Old text', { analysis_id: 7 }, '[DONE]'])))
    expect(await analyzeLensImage(file(), {})).toEqual({ text: 'Old text', analysisId: 7 })
  })
  it.each(['lens', 'scribe'])('propagates terminal %s errors instead of returning partial success', async domain => {
    vi.stubGlobal('fetch', vi.fn(async () => stream([
      { content: 'Partial draft' }, { error: 'Drafting failed. Please retry.', code: `${domain}_stream_failed`, terminal: true },
    ])))
    const failure = await (domain === 'lens' ? analyzeLensImage(file(), {}) : structureNote('demo', 'soap')).catch(error => error)
    expect(failure).toBeInstanceOf(ApiStreamError)
    expect(failure.code).toBe(`${domain}_stream_failed`)
  })
  it.each(['lens', 'scribe'])('rejects a truncated %s stream with no saved-record verdict', async domain => {
    vi.stubGlobal('fetch', vi.fn(async () => stream([{ content: 'Partial draft' }])))
    const failure = await (domain === 'lens' ? analyzeLensImage(file(), {}) : structureNote('demo', 'soap')).catch(error => error)
    expect(failure.code).toBe(`${domain}_stream_incomplete`)
  })
  it('refuses a stream response after another user signs in', async () => {
    setUser({ id: 'demo-A', name: 'Demo', role: 'doctor' })
    vi.stubGlobal('fetch', vi.fn(async () => {
      setUser({ id: 'demo-B', name: 'Other', role: 'doctor' })
      return stream([{ content: 'private findings' }, { analysis_id: 2 }, '[DONE]'])
    }))
    const chunks = vi.fn()
    await expect(analyzeLensImage(file(), {}, chunks)).rejects.toBeInstanceOf(ApiIdentityChangedError)
    expect(chunks).not.toHaveBeenCalled()
  })
})
