/**
 * THE KIOSK ROUTE, /display/opd.
 *
 * A token board is a DEVICE on a waiting-room wall, not a signed-in person.
 * The route therefore renders with no session at all, with a staff session,
 * and with a patient session, and never redirects to a sign-in screen or into
 * the staff shell. The page behind it is a placeholder until the board itself
 * is built (opd-9); what it may fetch is pinned here: nothing but the
 * unauthenticated /api/health, which carries no patient data.
 */
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  user: null as null | { id: string; name: string; role: string },
}))

vi.mock('../api/client', () => ({
  getUser: () => h.user,
  clearToken: vi.fn(),
  setUser: vi.fn(),
  deauthenticateSocket: vi.fn(),
  clearOfflineQueue: vi.fn(),
  logoutSession: vi.fn(async () => {}),
}))
vi.mock('../hooks/useIdleTimer', () => ({ useIdleTimer: () => {} }))
vi.mock('../hooks/useOfflineSession', () => ({ useOfflineSession: () => {} }))
vi.mock('../lib/nurseDrafts', () => ({ clearNurseDrafts: vi.fn() }))
vi.mock('../lib/offline/phiCache', () => ({ clearOfflineCache: vi.fn() }))
vi.mock('../lib/theme', () => ({ bootTheme: () => 'dark', storedTheme: () => null }))
vi.mock('./Login', () => ({ default: () => <div data-testid="page-login" /> }))
vi.mock('./ClinicianShell', () => ({ default: () => <div data-testid="page-shell" /> }))
vi.mock('./PatientShell', () => ({ default: () => <div data-testid="page-bridge" /> }))
vi.mock('./DischargeAccess', () => ({ default: () => <div data-testid="page-discharge" /> }))
vi.mock('./DowntimeMode', () => ({ default: () => <div data-testid="page-downtime" /> }))
vi.mock('./HandoffPage', () => ({ default: () => <div data-testid="page-handoff" /> }))
vi.mock('../components/SessionExpiredModal', () => ({ default: () => null }))

import '../i18n'
import App from '../AppRoot'
import { resetFeatureFlags, resetModuleFlags } from '../shellConfig'

function stubHealth(answer: 'down' | { modules: Record<string, boolean>; features: Record<string, boolean> }) {
  const spy = vi.fn(async (url: string) => {
    if (answer === 'down') throw new Error('offline')
    if (!String(url).endsWith('/api/health')) throw new Error(`unexpected fetch: ${url}`)
    return { ok: true, status: 200, json: async () => answer }
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

const OPD_OFF = { modules: { billing: true }, features: { registration: false, opd: false } }

beforeEach(() => {
  resetModuleFlags()
  resetFeatureFlags()
  window.matchMedia = window.matchMedia ?? ((() => ({
    matches: false, addEventListener: () => {}, removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia)
  window.history.pushState({}, '', '/display/opd#key=board-secret')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  h.user = null
  window.history.pushState({}, '', '/')
})

describe('/display/opd is a kiosk, not a signed-in page', () => {
  it.each([
    ['no session', null],
    ['a staff session', { id: 'ADM001', name: 'Asha Admin', role: 'admin' }],
    ['a patient session', { id: 'P1', name: 'Pat Portal', role: 'patient' }],
  ])('renders the board with %s, never a sign-in screen or a shell', async (_label, user) => {
    h.user = user
    stubHealth(OPD_OFF)
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Token board' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/display/opd')
    for (const page of ['login', 'shell', 'bridge']) {
      expect(screen.queryByTestId(`page-${page}`)).toBeNull()
    }
  })

  it('says the board is switched off while the opd feature is off', async () => {
    stubHealth(OPD_OFF)
    render(<App />)
    await waitFor(() => expect(screen.getByRole('status'))
      .toHaveTextContent('OPD token board is switched off on this site.'))
  })

  it('says it cannot open yet when the server has OPD on', async () => {
    stubHealth({ modules: { billing: true }, features: { registration: true, opd: true } })
    render(<App />)
    await waitFor(() => expect(screen.getByRole('status'))
      .toHaveTextContent('OPD token board cannot be opened on this site yet.'))
  })

  it('never claims "off" when the server cannot be reached', async () => {
    stubHealth('down')
    render(<App />)
    await waitFor(() => expect(screen.getByRole('status'))
      .toHaveTextContent('Orb could not reach the server'))
  })

  it('asks for nothing but /api/health, and never sends the board key anywhere', async () => {
    const spy = stubHealth(OPD_OFF)
    render(<App />)
    await screen.findByRole('heading', { name: 'Token board' })
    await waitFor(() => expect(spy).toHaveBeenCalled())
    for (const [url, init] of spy.mock.calls as unknown as Array<[string, RequestInit | undefined]>) {
      expect(String(url)).toMatch(/\/api\/health$/)
      expect(String(url)).not.toContain('board-secret')
      expect(JSON.stringify(init ?? {})).not.toContain('board-secret')
    }
  })
})
