/**
 * Single-flight session refresh (401 → refresh → retry once → hard expiry).
 *
 * The session lives in an HttpOnly cookie the client cannot read, so refresh is
 * REACTIVE-ONLY by design: any authenticated call that 401s triggers exactly one
 * POST /auth/refresh (shared by every concurrent 401), one retry of the original
 * request, and — only when the refresh or the retry fails — the established
 * `orb:session-expired` hard-stop. The invariant that no token ever lands in
 * localStorage must survive the whole flow.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiIdentityChangedError, apiFetch, getUser, setUser } from './client'

const events: string[] = []
const onExpired = () => events.push('expired')

beforeEach(() => {
  events.length = 0
  localStorage.clear()
  window.addEventListener('orb:session-expired', onExpired)
})

afterEach(() => {
  window.removeEventListener('orb:session-expired', onExpired)
  vi.unstubAllGlobals()
})

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

/**
 * Fetch stub modelling a lapsed session: API calls 401 until /auth/refresh has
 * succeeded, then 200. `refreshStatus` controls whether refresh can succeed;
 * `refreshDelayMs` keeps the refresh in flight long enough for concurrent 401
 * handlers to pile onto it.
 */
function stubLapsedSession({ refreshStatus = 200, refreshDelayMs = 0, repairSession = true } = {}) {
  let sessionValid = false
  const calls: string[] = []
  const impl = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url)
    calls.push(path)
    if (path.endsWith('/auth/refresh')) {
      if (refreshDelayMs) await new Promise((r) => setTimeout(r, refreshDelayMs))
      if (refreshStatus === 200 && repairSession) sessionValid = true
      return json(refreshStatus === 200 ? { token: 'x' } : { detail: 'Token expired' }, refreshStatus)
    }
    if (!sessionValid) return json({ detail: 'Token expired' }, 401)
    return json({ ok: true })
  })
  vi.stubGlobal('fetch', impl)
  return { calls }
}

const refreshCalls = (calls: string[]) => calls.filter((c) => c.endsWith('/auth/refresh')).length
const firstUser = { id: 'NUR-A', name: 'Nurse A', role: 'nurse' }
const nextUser = { id: 'NUR-B', name: 'Nurse B', role: 'nurse' }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

describe('single-flight session refresh', () => {
  it('refreshes once and retries the original request on a 401', async () => {
    const { calls } = stubLapsedSession()
    const result = await apiFetch<{ ok: boolean }>('/vigil/patients')
    expect(result.ok).toBe(true)
    // original → refresh → retry, nothing more.
    expect(calls).toHaveLength(3)
    expect(refreshCalls(calls)).toBe(1)
    expect(events).toHaveLength(0)
  })

  it('shares ONE refresh across concurrent 401s', async () => {
    const { calls } = stubLapsedSession({ refreshDelayMs: 20 })
    const [a, b, c] = await Promise.all([
      apiFetch<{ ok: boolean }>('/vigil/patients'),
      apiFetch<{ ok: boolean }>('/relay/channels'),
      apiFetch<{ ok: boolean }>('/sage/insights'),
    ])
    expect(a.ok && b.ok && c.ok).toBe(true)
    expect(refreshCalls(calls)).toBe(1)
    expect(events).toHaveLength(0)
  })

  it('hard-expires the session when the refresh itself fails', async () => {
    const { calls } = stubLapsedSession({ refreshStatus: 401 })
    await expect(apiFetch('/vigil/patients')).rejects.toThrow('Session expired')
    expect(events).toEqual(['expired'])
    // No blind retry of the original request after a failed refresh.
    expect(calls).toHaveLength(2)
  })

  it('retries exactly once — a 401 on the retry is a hard expiry, not a loop', async () => {
    const { calls } = stubLapsedSession({ repairSession: false })
    await expect(apiFetch('/vigil/patients')).rejects.toThrow('Session expired')
    expect(events).toEqual(['expired'])
    // original → refresh → single retry; never a second refresh.
    expect(calls).toHaveLength(3)
    expect(refreshCalls(calls)).toBe(1)
  })

  it('does not attempt a refresh for the sign-in endpoints', async () => {
    const { calls } = stubLapsedSession()
    await expect(apiFetch('/auth/login', { method: 'POST' })).rejects.toThrow()
    expect(refreshCalls(calls)).toBe(0)
    expect(events).toHaveLength(0)
  })

  it('never persists a token to localStorage anywhere in the flow', async () => {
    stubLapsedSession()
    await apiFetch('/vigil/patients')
    expect(localStorage.getItem('orb_token')).toBeNull()
  })

  it.each([200, 401])('does not publish an abandoned initial HTTP %s or expire the next user', async status => {
    setUser(firstUser)
    const response = deferred<Response>()
    const fetch = vi.fn(() => response.promise)
    vi.stubGlobal('fetch', fetch)
    const result = apiFetch('/vigil/patients').catch(e => e)
    setUser(nextUser)
    response.resolve(json(status === 200 ? [{ id: 'old-chart' }] : { detail: 'Expired' }, status))

    expect(await result).toBeInstanceOf(ApiIdentityChangedError)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(getUser()).toEqual(nextUser)
    expect(events).toEqual([])
  })

  it.each([200, 401])('does not retry or expire the next user after an abandoned HTTP %s refresh', async status => {
    setUser(firstUser)
    const refresh = deferred<Response>()
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ detail: 'Expired' }, 401))
      .mockImplementationOnce(() => refresh.promise)
    vi.stubGlobal('fetch', fetch)
    const result = apiFetch('/vigil/patients', { method: 'POST', body: '{}' }).catch(e => e)
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    setUser(nextUser)
    refresh.resolve(json({ ok: status === 200 }, status))

    expect(await result).toBeInstanceOf(ApiIdentityChangedError)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(getUser()).toEqual(nextUser)
    expect(events).toEqual([])
  })

  it.each([200, 401])('does not publish an abandoned HTTP %s retry or expire the next user', async status => {
    setUser(firstUser)
    const retry = deferred<Response>()
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ detail: 'Expired' }, 401))
      .mockResolvedValueOnce(json({ ok: true }))
      .mockImplementationOnce(() => retry.promise)
    vi.stubGlobal('fetch', fetch)
    const result = apiFetch('/vigil/patients').catch(e => e)
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    setUser(nextUser)
    retry.resolve(json(status === 200 ? [{ id: 'old-chart' }] : { detail: 'Expired' }, status))

    expect(await result).toBeInstanceOf(ApiIdentityChangedError)
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(getUser()).toEqual(nextUser)
    expect(events).toEqual([])
  })

  it.each([200, 403])('does not publish an old HTTP %s body after identity changes during body parsing', async status => {
    setUser(firstUser)
    const body = deferred<unknown>()
    const response = json({}, status)
    const parse = vi.spyOn(response, 'json').mockImplementation(() => body.promise)
    vi.stubGlobal('fetch', vi.fn(async () => response))
    const result = apiFetch('/vigil/patients').catch(e => e)
    await vi.waitFor(() => expect(parse).toHaveBeenCalledTimes(1))
    setUser(nextUser)
    body.resolve(status === 200 ? [{ id: 'old-chart' }] : { detail: 'Old permission refusal' })

    expect(await result).toBeInstanceOf(ApiIdentityChangedError)
    expect(getUser()).toEqual(nextUser)
    expect(events).toEqual([])
  })

  it('keeps anonymous sign-in responses usable when the stored identity changes', async () => {
    const response = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn(() => response.promise))
    const result = apiFetch('/auth/login', { method: 'POST', body: '{}' })
    setUser(nextUser)
    response.resolve(json({ user: nextUser }))
    await expect(result).resolves.toEqual({ user: nextUser })
    expect(events).toEqual([])
  })
})
