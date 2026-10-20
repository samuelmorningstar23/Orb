/**
 * A visiting consultant's sign-in never re-opens in place at another facility
 * (multisite-19, W13-E, ruling MS-R7 (a)).
 *
 * The password prompt (the idle "Screen locked" and the "Session expired"
 * one) signs in through POST /api/auth/login, which always lands at the
 * account's OWN facility. A visiting sign-in that went through it carried on
 * under the visiting facility's header and panels while the server answered
 * for home: a patient board from one facility under a header naming another.
 *
 * So, with the real AppRoot and the real shell:
 *   * a visiting sign-in that idles goes back to the sign-in screen, with no
 *     password prompt (the shell cancels AppRoot's "orb:idle-lock");
 *   * a home sign-in still locks and carries on in place;
 *   * whenever a re-entered password lands at a different facility than the
 *     screen was opened for (the shell could not ask where it works), Orb
 *     reloads instead of carrying on, and the prompt stays up until it does;
 *   * another tab moving the shared sign-in to another facility reloads this
 *     one, including a tab signed in through the real sign-in screen, and a
 *     tab whose stored sign-in names no facility (the switch's own signal);
 *   * with one facility on record nothing changes.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const idle = vi.hoisted(() => ({ onIdle: null as null | (() => void), enabled: false }))
const apiFetch = vi.hoisted(() => vi.fn())
const logoutSession = vi.hoisted(() => vi.fn(async () => {}))
const reloadPage = vi.hoisted(() => vi.fn())
const reauth = vi.hoisted(() => ({ user: null as Record<string, unknown> | null }))

vi.mock('./hooks/useIdleTimer', () => ({
  useIdleTimer: (_ms: number, onIdle: () => void, enabled = true) => {
    idle.onIdle = onIdle
    idle.enabled = enabled
  },
}))
vi.mock('./hooks/useOfflineSession', () => ({ useOfflineSession: () => {} }))
vi.mock('./lib/pageReload', () => ({ reloadPage: () => reloadPage() }))
vi.mock('./lib/offline/phiCache', () => ({ clearOfflineCache: () => 0 }))
vi.mock('./lib/nurseDrafts', () => ({ clearNurseDrafts: () => 0 }))

vi.mock('./api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
  getUser: () => {
    const raw = localStorage.getItem('orb_user')
    return raw ? JSON.parse(raw) : null
  },
  setUser: (user: unknown) => localStorage.setItem('orb_user', JSON.stringify(user)),
  clearToken: () => localStorage.removeItem('orb_user'),
  deauthenticateSocket: vi.fn(),
  clearOfflineQueue: vi.fn(),
  clearForeignOfflineQueue: vi.fn(),
  logoutSession: () => logoutSession(),
  authenticateSocket: vi.fn(),
  // POST /api/auth/login as the real client runs it: the server's user body
  // goes into the stored sign-in.
  login: vi.fn(async () => {
    localStorage.setItem('orb_user', JSON.stringify(reauth.user))
    return { token: 'x', user: reauth.user }
  }),
  getPatientDetail: vi.fn(async () => null),
  getAdministrations: vi.fn(async () => []),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  dischargePatient: vi.fn(), addClinicalNote: vi.fn(), addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getDischargeQrObjectUrl: vi.fn(), prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
  getVitalsFeed: vi.fn(async () => ({ simulated: false })),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

vi.mock('./pages/PatientShell', () => ({ default: () => <div>Patient shell</div> }))
// The real sign-in screen: what it stores is what every tab compares. This is
// a workstation, not an enrolled ward tablet.
vi.mock('./api/deviceAuth', () => ({
  getDeviceStatus: vi.fn(async () => ({ enrolled: false })),
  bindDevice: vi.fn(),
  pinLogin: vi.fn(),
}))
vi.mock('./components/Aurora', () => ({ default: () => null }))
vi.mock('./pages/DischargeAccess', () => ({ default: () => null }))
vi.mock('./pages/DowntimeMode', () => ({ default: () => null }))
vi.mock('./pages/HandoffPage', () => ({ default: () => null }))
vi.mock('./pages/OpdTokenBoard', () => ({ default: () => null }))

// Shell chrome that is irrelevant here.
vi.mock('./components/AdmitPatientModal', () => ({ default: () => null }))
vi.mock('./components/PulseWidget', () => ({ default: () => null }))
vi.mock('./components/ClockWidget', () => ({ default: () => null }))
vi.mock('./components/AiStatusChip', () => ({ default: () => null }))
vi.mock('./components/sage/SageFloatingAssistant', () => ({ default: () => null }))
vi.mock('./components/CommandPalette', () => ({ default: () => null }))
vi.mock('./components/AlertPermissionButton', () => ({ default: () => null }))
vi.mock('./components/MfaEnrollmentModal', () => ({ default: () => null }))
vi.mock('./services/criticalAlerts', () => ({ initCriticalAlerts: () => () => {} }))
vi.mock('./components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('./components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('./components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('./components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('./components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import App from './AppRoot'
import { announceFacilitySwitch } from './lib/facilitySwitchSignal'
import { markSwitchesKnownForTests, resetPlatformFlags, setPlatformFlags } from './shellConfig'

const HOME = { id: 1, code: 'MAIN', name: 'Quorvane General' }
const SATELLITE = { id: 9, code: 'HLV1', name: 'Halvenor Satellite' }

const AT_HOME = { id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Quorvane Medicine', facility: HOME }
const VISITING = { id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Halvenor Cardiology', facility: SATELLITE }

function facilities(visiting: boolean) {
  return {
    active_facility: visiting ? SATELLITE : HOME,
    visiting,
    can_switch: true,
    reason: null,
    options: [
      { facility: HOME, department: 'Quorvane Medicine', kind: 'home', active: !visiting },
      { facility: SATELLITE, department: 'Halvenor Cardiology', kind: 'visiting', active: visiting },
    ],
  }
}

/** What GET /api/auth/facilities answers; a function so a test can make it fail. */
let sessionFacilities: () => Promise<unknown>
/** The user body POST /api/auth/login answers the sign-in screen with. */
let signInUser: Record<string, unknown>

beforeEach(() => {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }))
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
  markSwitchesKnownForTests()
  resetPlatformFlags()
  idle.onIdle = null
  idle.enabled = false
  apiFetch.mockReset()
  logoutSession.mockClear()
  reloadPage.mockReset()
  localStorage.clear()
  reauth.user = AT_HOME
  sessionFacilities = async () => facilities(false)
  signInUser = {
    id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Quorvane Medicine',
    registration_number: 'KMC-40417', facility: HOME,
  }
  apiFetch.mockImplementation(async (path: string) => {
    const stored = JSON.parse(localStorage.getItem('orb_user') || 'null')
    if (path === '/auth/me') return { facility: stored?.facility ?? null }
    if (path === '/auth/facilities') return sessionFacilities()
    if (path === '/auth/login') return { token: 'x', user: signInUser }
    return []
  })
})

afterEach(() => {
  resetPlatformFlags()
  vi.unstubAllGlobals()
  localStorage.clear()
})

function signedIn(user: Record<string, unknown>) {
  localStorage.setItem('orb_user', JSON.stringify(user))
}

async function shell() {
  const view = render(<App />)
  await waitFor(() => expect(view.container.querySelector('.shell__user-role')).toBeTruthy())
  return view
}

function roleLine(container: HTMLElement): string {
  return (container.querySelector('.shell__user-role') as HTMLElement).textContent ?? ''
}

function goIdle() {
  expect(idle.enabled).toBe(true)
  act(() => { idle.onIdle?.() })
}

/** Signs in through the real sign-in screen, as a person at a workstation does. */
async function signInThroughScreen() {
  window.history.pushState({}, '', '/login/staff')
  const view = render(<App />)
  await waitFor(() => expect(document.getElementById('input-employee-id')).toBeTruthy())
  fireEvent.change(document.getElementById('input-employee-id') as HTMLElement, { target: { value: 'DOC001' } })
  fireEvent.change(document.getElementById('input-password') as HTMLElement, { target: { value: 'quill-harbour-9' } })
  await act(async () => { fireEvent.click(document.getElementById('btn-login') as HTMLElement) })
  await waitFor(() => expect(view.container.querySelector('.shell__user-role')).toBeTruthy())
  return view
}

/** The storage event this tab sees when another tab of the browser writes `key`. */
function anotherTabWrote(key: string) {
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key, newValue: localStorage.getItem(key) }))
  })
}

async function reenterPassword() {
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'quill-harbour-9' } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
}

describe('a visiting sign-in and the password prompt', () => {
  it('a visiting sign-in that idles goes back to the sign-in screen with no password prompt', async () => {
    signedIn(VISITING)
    sessionFacilities = async () => facilities(true)
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(roleLine(container)).toContain('Visiting'))

    goIdle()

    expect(screen.queryByRole('heading', { name: 'Screen locked' })).toBeNull()
    expect(logoutSession).toHaveBeenCalledTimes(1)
    expect(reloadPage).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem('orb_user')).toBeNull()
    await waitFor(() => expect(document.getElementById('input-employee-id')).toBeTruthy())
  })

  it('a home sign-in that idles still locks and carries on in place', async () => {
    signedIn(AT_HOME)
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(apiFetch.mock.calls.some(c => c[0] === '/auth/facilities')).toBe(true))

    goIdle()
    expect(screen.getByRole('heading', { name: 'Screen locked' })).toBeInTheDocument()
    await reenterPassword()

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Screen locked' })).toBeNull())
    expect(reloadPage).not.toHaveBeenCalled()
    expect(logoutSession).not.toHaveBeenCalled()
    expect(roleLine(container)).toContain('Quorvane Medicine')
  })

  it('reloads when a re-entered password at the idle lock lands at another facility', async () => {
    // The shell could not ask where this sign-in works, so it did not cancel
    // the lock; the re-entered password then signed in at home.
    signedIn(VISITING)
    sessionFacilities = async () => { throw new Error('unreachable') }
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(apiFetch.mock.calls.some(c => c[0] === '/auth/facilities')).toBe(true))
    expect(roleLine(container)).toContain('Halvenor Cardiology')

    goIdle()
    expect(screen.getByRole('heading', { name: 'Screen locked' })).toBeInTheDocument()
    reauth.user = AT_HOME
    await reenterPassword()

    await waitFor(() => expect(reloadPage).toHaveBeenCalledTimes(1))
    // Not carried on in place: the prompt stays over the visiting screens
    // until the reload, and the stored sign-in is the home one it boots into.
    expect(screen.getByRole('heading', { name: 'Screen locked' })).toBeInTheDocument()
    expect(roleLine(container)).toContain('Halvenor Cardiology')
    expect(JSON.parse(localStorage.getItem('orb_user') || 'null')).toMatchObject({
      department: 'Quorvane Medicine', facility: HOME,
    })
  })

  it('reloads when a re-entered password at the expired-session prompt lands at another facility', async () => {
    signedIn(VISITING)
    sessionFacilities = async () => { throw new Error('unreachable') }
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    await shell()
    await waitFor(() => expect(apiFetch.mock.calls.some(c => c[0] === '/auth/facilities')).toBe(true))

    act(() => { window.dispatchEvent(new CustomEvent('orb:session-expired')) })
    expect(screen.getByRole('heading', { name: 'Session expired' })).toBeInTheDocument()
    reauth.user = AT_HOME
    await reenterPassword()

    await waitFor(() => expect(reloadPage).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('heading', { name: 'Session expired' })).toBeInTheDocument()
  })

  it('with one facility on record a re-entered password carries on in place', async () => {
    const single = { ...AT_HOME, facility: { id: 1, code: 'MAIN', name: 'Quorvane General' } }
    signedIn(single)
    reauth.user = single
    await shell()

    goIdle()
    expect(screen.getByRole('heading', { name: 'Screen locked' })).toBeInTheDocument()
    await reenterPassword()

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Screen locked' })).toBeNull())
    expect(reloadPage).not.toHaveBeenCalled()
    expect(apiFetch.mock.calls.some(c => c[0] === '/auth/facilities')).toBe(false)
  })

  it('reloads when another tab moves the shared sign-in to another facility', async () => {
    signedIn(AT_HOME)
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    await shell()

    // Same facility, or somebody else signing in elsewhere: nothing here.
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'orb_user' })) })
    signedIn({ ...VISITING, id: 'n7', name: 'Ysolde Brannick', role: 'nurse' })
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'orb_user' })) })
    expect(reloadPage).not.toHaveBeenCalled()

    signedIn(VISITING)
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'orb_user' })) })
    expect(reloadPage).toHaveBeenCalledTimes(1)
  })

  it('a tab signed in through the sign-in screen reloads when another tab moves the sign-in', async () => {
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    await signInThroughScreen()
    // The sign-in screen keeps where the sign-in works, as the server said it.
    expect(JSON.parse(localStorage.getItem('orb_user') || 'null')).toMatchObject({
      id: 'd1', department: 'Quorvane Medicine', facility: HOME,
    })

    // Another tab of this browser moves the shared sign-in to the satellite.
    signedIn(VISITING)
    anotherTabWrote('orb_user')
    expect(reloadPage).toHaveBeenCalledTimes(1)
  })

  it("a tab whose sign-in names no facility still reloads on another tab's switch", async () => {
    // The server could not say where this sign-in works, so the stored
    // sign-in cannot show a move; the switch's own signal does.
    delete signInUser.facility
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    await signInThroughScreen()

    // What the switching tab writes: its new sign-in, then the signal.
    signedIn(VISITING)
    anotherTabWrote('orb_user')
    expect(reloadPage).not.toHaveBeenCalled()
    announceFacilitySwitch()
    anotherTabWrote('orb_facility_switch')
    expect(reloadPage).toHaveBeenCalledTimes(1)
    // One reload is enough; a later event does not ask again.
    anotherTabWrote('orb_facility_switch')
    expect(reloadPage).toHaveBeenCalledTimes(1)
  })

  it("a sign-in stored by an older build, with no facility, reloads on another tab's switch", async () => {
    const stored: Record<string, unknown> = { ...AT_HOME }
    delete stored.facility
    signedIn(stored)
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    await shell()

    signedIn(VISITING)
    announceFacilitySwitch()
    anotherTabWrote('orb_user')
    anotherTabWrote('orb_facility_switch')
    expect(reloadPage).toHaveBeenCalledTimes(1)
  })

  it("a tab nobody is signed in to is left alone by another tab's switch", async () => {
    window.history.pushState({}, '', '/login/staff')
    render(<App />)
    await waitFor(() => expect(document.getElementById('input-employee-id')).toBeTruthy())

    signedIn(VISITING)
    announceFacilitySwitch()
    anotherTabWrote('orb_user')
    anotherTabWrote('orb_facility_switch')
    expect(reloadPage).not.toHaveBeenCalled()
  })
})
