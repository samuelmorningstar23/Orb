/**
 * A wrong PIN is not an expired session.
 *
 * `apiFetch` treats any 401 as "your session died": it fires the
 * `orb:session-expired` event (App latches it and throws the blocking re-auth
 * modal up) and replaces the server's message with a flat "Session expired".
 * That is right for a request made WITH a session and wrong for the sign-in
 * endpoints, which authenticate a caller who has none yet.
 *
 * Left unfixed it produced two visible faults on a ward tablet:
 *
 *   1. A nurse mistypes her PIN, gets 401, then types it correctly — and the
 *      "your session expired, sign in again" modal appears over the dashboard
 *      she just reached, because the flag latched on the failed attempt.
 *   2. The sign-in screen shows "Session expired" instead of "Invalid staff ID
 *      or PIN" or "This device is not enrolled" — so it cannot tell the user
 *      what to do, and cannot fall back to the password form.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'

const events: string[] = []
const onExpired = () => events.push('expired')

beforeEach(() => {
  events.length = 0
  window.addEventListener('orb:session-expired', onExpired)
})

afterEach(() => {
  window.removeEventListener('orb:session-expired', onExpired)
  vi.unstubAllGlobals()
})

function stub401(detail: string) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(
    JSON.stringify({ detail }), { status: 401, headers: { 'Content-Type': 'application/json' } },
  )))
}

describe('401 handling on the sign-in endpoints', () => {
  it('does not treat a wrong PIN as an expired session', async () => {
    stub401('Invalid staff ID or PIN')
    await expect(apiFetch('/auth/pin-login', { method: 'POST' }))
      .rejects.toThrow('Invalid staff ID or PIN')
    expect(events).toHaveLength(0)
  })

  it('surfaces the "device not enrolled" reason so the screen can fall back', async () => {
    stub401('This device is not enrolled for PIN sign-in. Use your password.')
    await expect(apiFetch('/auth/pin-login', { method: 'POST' }))
      .rejects.toThrow(/not enrolled/)
    expect(events).toHaveLength(0)
  })

  it('does not fire on the device bind or status checks either', async () => {
    stub401('This device credential is not valid.')
    await expect(apiFetch('/auth/device/bind', { method: 'POST' })).rejects.toThrow()
    await expect(apiFetch('/auth/device')).rejects.toThrow()
    expect(events).toHaveLength(0)
  })

  it('still fires for a genuine session 401 on any other endpoint', async () => {
    stub401('Session revoked; please log in again')
    await expect(apiFetch('/vigil/patients')).rejects.toThrow('Session expired')
    expect(events).toEqual(['expired'])
  })
})
