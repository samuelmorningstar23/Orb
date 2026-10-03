/**
 * WIRE-LEVEL proof for the eMAR double-charge.
 *
 * The modal test asserts what the component hands to the client. This asserts
 * what actually goes on the wire, because the Idempotency-Key header is the
 * only thing the server can dedupe on (emar_routes.py:50 reads it, and returns
 * the existing record when it has seen the key before).
 *
 * Before the fix, offlineMutate minted the key itself on every call, so two
 * attempts at the SAME dose — which is what a nurse retrying a lost-response
 * failure produces — went out under two different keys and the server had no
 * way to tell they were one dose.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { administerMedication } from './client'

function captureKeys() {
  const keys: (string | undefined)[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
    const h = (init.headers || {}) as Record<string, string>
    keys.push(h['Idempotency-Key'])
    return {
      ok: true, status: 200,
      json: async () => ({ status: 'ok', administration: { id: 1 } }),
      headers: { get: () => null },
    }
  }))
  return keys
}

describe('administerMedication Idempotency-Key on the wire', () => {
  beforeEach(() => {
    localStorage.setItem('orb_user', JSON.stringify({ id: 'n1', name: 'Nurse', role: 'nurse' }))
    vi.stubGlobal('navigator', { ...globalThis.navigator, onLine: true })
  })

  it('sends the caller key verbatim, so a retry of one dose dedupes', async () => {
    const keys = captureKeys()
    const key = 'dose-attempt-1'
    await administerMedication(42, { status: 'given', confirmation_method: 'typed' }, key)
    await administerMedication(42, { status: 'given', confirmation_method: 'typed' }, key)

    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(key)
    expect(keys[1]).toBe(key)
  })

  it('still mints a key when the caller supplies none', async () => {
    const keys = captureKeys()
    await administerMedication(42, { status: 'given', confirmation_method: 'typed' })
    expect(typeof keys[0]).toBe('string')
    expect(keys[0]).toBeTruthy()
  })
})
