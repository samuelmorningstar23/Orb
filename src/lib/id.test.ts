import { afterEach, describe, expect, it, vi } from 'vitest'
import { newId } from './id'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('newId', () => {
  it('uses crypto.randomUUID when the page is a secure context', () => {
    const randomUUID = vi.fn(() => '11111111-2222-4333-8444-555555555555')
    vi.stubGlobal('crypto', { randomUUID, getRandomValues: vi.fn() })
    expect(newId()).toBe('11111111-2222-4333-8444-555555555555')
    expect(randomUUID).toHaveBeenCalledTimes(1)
  })

  it('still makes an id over plain http on a LAN, where randomUUID is undefined', () => {
    const real = globalThis.crypto
    vi.stubGlobal('crypto', { getRandomValues: (b: Uint8Array) => real.getRandomValues(b) })
    const a = newId()
    const b = newId()
    expect(a).toMatch(UUID_V4)
    expect(b).toMatch(UUID_V4)
    expect(a).not.toBe(b)
  })

  it('falls back when randomUUID is exposed but throws', () => {
    const real = globalThis.crypto
    vi.stubGlobal('crypto', {
      randomUUID: () => { throw new Error('insecure context') },
      getRandomValues: (b: Uint8Array) => real.getRandomValues(b),
    })
    expect(newId()).toMatch(UUID_V4)
  })

  it('makes an id with no crypto at all', () => {
    vi.stubGlobal('crypto', undefined)
    expect(newId()).toMatch(UUID_V4)
  })
})
