/**
 * Sign-out reaches the server.
 *
 * Signing out used to clear only local state: the HttpOnly session cookie
 * stayed valid, and on the next page load the orb_csrf cookie made the socket
 * reconnect as the previous user. logoutSession posts to /auth/logout with the
 * CSRF header, never latches the expired-session modal (a 401 there means the
 * session is already dead, which is what sign-out wants), never throws, and
 * expires orb_csrf afterwards unless a new sign-in has replaced it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { logoutSession } from './client'

const events: string[] = []
const onExpired = () => events.push('expired')

function setCsrf(value: string) {
  document.cookie = `orb_csrf=${value}; path=/`
}
function csrf(): string | null {
  const m = document.cookie.match(/(?:^|;\s*)orb_csrf=([^;]+)/)
  return m ? m[1] : null
}

beforeEach(() => {
  events.length = 0
  localStorage.clear()
  window.addEventListener('orb:session-expired', onExpired)
})

afterEach(() => {
  window.removeEventListener('orb:session-expired', onExpired)
  vi.unstubAllGlobals()
  document.cookie = 'orb_csrf=; Max-Age=0; path=/'
})

describe('logoutSession', () => {
  it('posts to /auth/logout with the session cookie and the CSRF header', async () => {
    setCsrf('tok-123')
    const fetchMock = vi.fn(async () => new Response('{"status":"logged_out"}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    expect(await logoutSession()).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/api\/auth\/logout$/)
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include')
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('tok-123')
    expect(csrf()).toBeNull()
  })

  it('a 401 is a dead session, not an expiry: no refresh, no modal', async () => {
    setCsrf('tok-dead')
    const fetchMock = vi.fn(async () => new Response('{"detail":"Token expired"}', { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)

    expect(await logoutSession()).toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as unknown as [string]
    expect(url).not.toMatch(/refresh/)
    expect(events).toHaveLength(0)
    expect(csrf()).toBeNull()
  })

  it('an unreachable server still lets sign-out finish', async () => {
    setCsrf('tok-offline')
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    await expect(logoutSession()).resolves.toBe(false)
    expect(events).toHaveLength(0)
    expect(csrf()).toBeNull()
  })

  it('leaves a newer sign-in alone when the old request settles late', async () => {
    setCsrf('tok-old')
    let release: (r: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(res => { release = res })))

    const pending = logoutSession()
    setCsrf('tok-new-session')
    release(new Response('{}', { status: 500 }))
    await pending
    expect(csrf()).toBe('tok-new-session')
  })
})
