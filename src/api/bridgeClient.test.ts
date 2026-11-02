import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cancelAppointment, getCareTeam, requestAppointment } from './bridgeClient'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

beforeEach(() => {
  localStorage.clear()
  document.cookie = 'orb_csrf=portal-test; path=/'
})

afterEach(() => {
  document.cookie = 'orb_csrf=; Max-Age=0; path=/'
  vi.unstubAllGlobals()
})

describe('Bridge cookie session', () => {
  it('refreshes an expired portal session and retries the read once', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ detail: 'Expired' }, 401))
      .mockResolvedValueOnce(json({ ok: true }))
      .mockResolvedValueOnce(json({ care_team: [], unit: {} }))
    vi.stubGlobal('fetch', fetch)

    expect(await getCareTeam()).toEqual({ care_team: [], unit: {} })
    expect(fetch.mock.calls.map(([url]) => new URL(String(url)).pathname)).toEqual([
      '/api/bridge/care-team', '/api/auth/refresh', '/api/bridge/care-team',
    ])
    for (const [, init] of fetch.mock.calls) expect(init.credentials).toBe('include')
  })

  it('sends the CSRF cookie on appointment writes and keeps IDs within one path segment', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(json({ id: 'A1', status: 'requested' })))
    vi.stubGlobal('fetch', fetch)
    await requestAppointment({ requested_date: '2026-10-20' })
    await cancelAppointment('A1/other?x=1')

    expect(fetch.mock.calls[0][1].headers['X-CSRF-Token']).toBe('portal-test')
    expect(fetch.mock.calls[1][1].headers['X-CSRF-Token']).toBe('portal-test')
    expect(String(fetch.mock.calls[1][0])).toMatch(/\/appointments\/A1%2Fother%3Fx%3D1$/)
  })
})
