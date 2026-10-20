/**
 * Visiting consultants: the session facility switch in the shell (multisite-19, W13-E).
 *
 * Ruling MS-R7 (a): one account, one facility per sign-in. The shell asks
 * GET /api/auth/facilities only while ORB_MULTI_SITE is on and more than one
 * facility is on record, offers "Switch facility" only when the server says
 * this sign-in may move, and after a move stores the new department and
 * facility, clears the facility being left out of this browser and reloads.
 * A move waits while offline changes are still queued. A visiting sign-in that
 * ends, idles, or is found ended by a switch that then fails goes back to the
 * sign-in screen: the password prompt would sign in at the account's own
 * facility while the visiting facility's screens stay open.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const authState = vi.hoisted(() => ({
  user: { id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Quorvane Medicine' } as Record<string, unknown>,
}))
const apiFetch = vi.hoisted(() => vi.fn())
const login = vi.hoisted(() => vi.fn())
const logout = vi.hoisted(() => vi.fn())
const reloadPage = vi.hoisted(() => vi.fn())
const clearOfflineCache = vi.hoisted(() => vi.fn(() => 0))
const clearNurseDrafts = vi.hoisted(() => vi.fn(() => 0))

vi.mock('../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
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
  getUser: () => ({ id: 'd1', name: 'Tavish Orrenby', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
  getVitalsFeed: vi.fn(async () => ({ simulated: false })),
}))

vi.mock('../App', () => ({
  useAuth: () => ({ user: authState.user, logout, login }),
}))
vi.mock('../lib/pageReload', () => ({ reloadPage: () => reloadPage() }))
vi.mock('../lib/offline/phiCache', () => ({ clearOfflineCache: () => clearOfflineCache() }))
vi.mock('../lib/nurseDrafts', () => ({ clearNurseDrafts: () => clearNurseDrafts() }))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

// Chrome that is irrelevant to the switch.
vi.mock('../components/AdmitPatientModal', () => ({ default: () => null }))
vi.mock('../components/PulseWidget', () => ({ default: () => null }))
vi.mock('../components/ClockWidget', () => ({ default: () => null }))
vi.mock('../components/AiStatusChip', () => ({ default: () => null }))
vi.mock('../components/sage/SageFloatingAssistant', () => ({ default: () => null }))
vi.mock('../components/CommandPalette', () => ({ default: () => null }))
vi.mock('../components/AlertPermissionButton', () => ({ default: () => null }))
vi.mock('../components/MfaEnrollmentModal', () => ({ default: () => null }))
vi.mock('../services/criticalAlerts', () => ({ initCriticalAlerts: () => () => {} }))
vi.mock('../components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import ClinicianShell from './ClinicianShell'
import { markSwitchesKnownForTests, resetPlatformFlags, setPlatformFlags } from '../shellConfig'

const HOME = { id: 1, code: 'MAIN', name: 'Quorvane General' }
const SATELLITE = { id: 9, code: 'HLV1', name: 'Halvenor Satellite' }

function facilities(overrides: Record<string, unknown> = {}) {
  return {
    active_facility: HOME,
    visiting: false,
    can_switch: true,
    reason: null,
    options: [
      { facility: HOME, department: 'Quorvane Medicine', kind: 'home', active: true },
      { facility: SATELLITE, department: 'Halvenor Cardiology', kind: 'visiting', active: false },
    ],
    ...overrides,
  }
}

let answers: { facilities: unknown, switch: () => Promise<unknown> }

const calls = (path: string) => apiFetch.mock.calls.filter(c => c[0] === path)

beforeEach(() => {
  markSwitchesKnownForTests()
  resetPlatformFlags()
  apiFetch.mockReset()
  login.mockReset()
  logout.mockReset()
  reloadPage.mockReset()
  clearOfflineCache.mockClear()
  clearNurseDrafts.mockClear()
  localStorage.removeItem('orb_offline_queue')
  localStorage.removeItem('orb_facility_switch')
  authState.user ={ id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Quorvane Medicine' }
  answers = {
    facilities: facilities(),
    switch: async () => ({
      token: 'x',
      user: { department: 'Halvenor Cardiology', facility: SATELLITE },
    }),
  }
  apiFetch.mockImplementation(async (path: string) => {
    if (path === '/auth/me') return { facility: HOME }
    if (path === '/auth/facilities') return answers.facilities
    if (path === '/auth/facility') return answers.switch()
    return []
  })
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

afterEach(() => {
  resetPlatformFlags()
  localStorage.removeItem('orb_offline_queue')
  localStorage.removeItem('orb_facility_switch')
})

async function shell() {
  const view = render(<ClinicianShell />)
  await waitFor(() => expect(view.container.querySelector('.shell__user-role')).toBeTruthy())
  return view
}

/** The dialog's own Switch button (the sidebar trigger shares its name). */
function confirmButton() {
  return within(screen.getByRole('dialog')).getByRole('button', { name: 'Switch facility' })
}

describe('the session facility switch', () => {
  it('asks nothing and offers nothing with one facility on record', async () => {
    setPlatformFlags({ multi_site: true })
    const { container } = await shell()
    expect(calls('/auth/facilities')).toHaveLength(0)
    expect(container.querySelector('#btn-switch-facility')).toBeNull()
  })

  it('asks nothing while ORB_MULTI_SITE is off, even with two facilities on record', async () => {
    setPlatformFlags({ multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(calls('/auth/me')).toHaveLength(1))
    expect(calls('/auth/facilities')).toHaveLength(0)
    expect(container.querySelector('#btn-switch-facility')).toBeNull()
  })

  it('offers nothing when the server says this sign-in may not move', async () => {
    answers.facilities = facilities({ can_switch: false, reason: 'Only a doctor with visiting access can work at another facility.' })
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(calls('/auth/facilities')).toHaveLength(1))
    expect(container.querySelector('#btn-switch-facility')).toBeNull()
  })

  it('moves the sign-in, stores the new facility, clears the old one and reloads', async () => {
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(container.querySelector('#btn-switch-facility')).toBeTruthy())
    fireEvent.click(container.querySelector('#btn-switch-facility') as HTMLElement)
    const dialog = await screen.findByRole('dialog', { name: 'Work at another facility' })
    const current = screen.getByRole('button', { name: /Quorvane General · Quorvane Medicine/ })
    expect(current).toBeDisabled()
    expect(current.textContent).toContain('Current')
    fireEvent.click(screen.getByRole('button', { name: /Halvenor Satellite · Halvenor Cardiology/ }))
    expect(dialog.textContent).toContain('Switch to Halvenor Satellite, Halvenor Cardiology?')
    fireEvent.click(confirmButton())
    await waitFor(() => expect(reloadPage).toHaveBeenCalledTimes(1))
    const [path, options] = calls('/auth/facility')[0]
    expect(path).toBe('/auth/facility')
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({ facility_id: 9 })
    expect(login).toHaveBeenCalledWith(expect.objectContaining({
      id: 'd1', department: 'Halvenor Cardiology', facility: SATELLITE,
    }))
    expect(clearOfflineCache).toHaveBeenCalledTimes(1)
    expect(clearNurseDrafts).toHaveBeenCalledTimes(1)
    // This browser's other tabs share the sign-in cookie and are told to
    // reload; the signal names nobody.
    const signal = localStorage.getItem('orb_facility_switch')
    expect(signal).toBeTruthy()
    expect(signal).not.toContain('d1')
  })

  it('moves home with a null facility', async () => {
    authState.user = { id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Halvenor Cardiology' }
    answers.facilities = facilities({
      visiting: true,
      options: [
        { facility: HOME, department: 'Quorvane Medicine', kind: 'home', active: false },
        { facility: SATELLITE, department: 'Halvenor Cardiology', kind: 'visiting', active: true },
      ],
    })
    answers.switch = async () => ({ token: 'x', user: { department: 'Quorvane Medicine', facility: HOME } })
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(container.querySelector('#btn-switch-facility')).toBeTruthy())
    fireEvent.click(container.querySelector('#btn-switch-facility') as HTMLElement)
    fireEvent.click(await screen.findByRole('button', { name: /Quorvane General · Quorvane Medicine/ }))
    fireEvent.click(confirmButton())
    await waitFor(() => expect(reloadPage).toHaveBeenCalledTimes(1))
    expect(JSON.parse(calls('/auth/facility')[0][1].body)).toEqual({ facility_id: null })
  })

  it('waits while offline changes are still queued', async () => {
    localStorage.setItem('orb_offline_queue', JSON.stringify([{ path: '/x' }]))
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(container.querySelector('#btn-switch-facility')).toBeTruthy())
    fireEvent.click(container.querySelector('#btn-switch-facility') as HTMLElement)
    fireEvent.click(await screen.findByRole('button', { name: /Halvenor Satellite/ }))
    expect(within(screen.getByRole('dialog')).getByRole('status').textContent)
      .toContain('still waiting to sync')
    const go = confirmButton()
    expect(go).toBeDisabled()
    fireEvent.click(go)
    expect(calls('/auth/facility')).toHaveLength(0)
    expect(reloadPage).not.toHaveBeenCalled()
  })

  it('shows the refusal and changes nothing when the server says no', async () => {
    answers.switch = async () => { throw new Error('Your account has no live visiting access at that facility.') }
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(container.querySelector('#btn-switch-facility')).toBeTruthy())
    fireEvent.click(container.querySelector('#btn-switch-facility') as HTMLElement)
    fireEvent.click(await screen.findByRole('button', { name: /Halvenor Satellite/ }))
    fireEvent.click(confirmButton())
    expect((await within(screen.getByRole('dialog')).findByRole('alert')).textContent)
      .toBe('Your account has no live visiting access at that facility.')
    expect(login).not.toHaveBeenCalled()
    expect(reloadPage).not.toHaveBeenCalled()
    expect(clearOfflineCache).not.toHaveBeenCalled()
    expect(localStorage.getItem('orb_facility_switch')).toBeNull()
  })

  it('says Visiting in the user chip, and a visiting sign-in that ends signs out', async () => {
    authState.user = { id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Halvenor Cardiology' }
    answers.facilities = facilities({ visiting: true })
    apiFetch.mockImplementation(async (path: string) => {
      if (path === '/auth/me') return { facility: SATELLITE }
      if (path === '/auth/facilities') return answers.facilities
      return []
    })
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    const role = container.querySelector('.shell__user-role') as HTMLElement
    await waitFor(() => expect(role.textContent).toBe('role.doctor · Halvenor Cardiology · Halvenor Satellite · Visiting'))
    act(() => { window.dispatchEvent(new CustomEvent('orb:session-expired')) })
    expect(logout).toHaveBeenCalledTimes(1)
    expect(reloadPage).toHaveBeenCalledTimes(1)
  })

  it('leaves an ending home sign-in to the usual re-sign-in', async () => {
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(container.querySelector('#btn-switch-facility')).toBeTruthy())
    act(() => { window.dispatchEvent(new CustomEvent('orb:session-expired')) })
    expect(logout).not.toHaveBeenCalled()
    expect(reloadPage).not.toHaveBeenCalled()
    const role = container.querySelector('.shell__user-role') as HTMLElement
    expect(role.textContent).not.toContain('Visiting')
  })

  /** The idle lock as AppRoot raises it: cancelable, and it locks only when
   *  no listener cancelled it. */
  function idleLock(): CustomEvent {
    const event = new CustomEvent('orb:idle-lock', { cancelable: true })
    act(() => { window.dispatchEvent(event) })
    return event
  }

  async function visitingShell() {
    authState.user = {
      id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Halvenor Cardiology', facility: SATELLITE,
    }
    answers.facilities = facilities({
      visiting: true,
      options: [
        { facility: HOME, department: 'Quorvane Medicine', kind: 'home', active: false },
        { facility: SATELLITE, department: 'Halvenor Cardiology', kind: 'visiting', active: true },
      ],
    })
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const view = await shell()
    const role = view.container.querySelector('.shell__user-role') as HTMLElement
    await waitFor(() => expect(role.textContent).toContain('Visiting'))
    return view
  }

  it('a visiting sign-in that idles signs out instead of locking for a password', async () => {
    await visitingShell()
    const event = idleLock()
    // Cancelled, so AppRoot raises no "Screen locked" prompt, whose password
    // would sign in at the home facility under the visiting facility's screens.
    expect(event.defaultPrevented).toBe(true)
    expect(logout).toHaveBeenCalledTimes(1)
    expect(reloadPage).toHaveBeenCalledTimes(1)
    expect(login).not.toHaveBeenCalled()
  })

  it('leaves an idle home sign-in to the usual screen lock', async () => {
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(container.querySelector('#btn-switch-facility')).toBeTruthy())
    const event = idleLock()
    expect(event.defaultPrevented).toBe(false)
    expect(logout).not.toHaveBeenCalled()
    expect(reloadPage).not.toHaveBeenCalled()
  })

  it('leaves the idle lock alone with ORB_MULTI_SITE off', async () => {
    authState.user = { id: 'd1', name: 'Tavish Orrenby', role: 'doctor', department: 'Halvenor Cardiology' }
    answers.facilities = facilities({ visiting: true })
    setPlatformFlags({ multi_facility_data: true })
    await shell()
    await waitFor(() => expect(calls('/auth/me')).toHaveLength(1))
    const event = idleLock()
    expect(event.defaultPrevented).toBe(false)
    expect(logout).not.toHaveBeenCalled()
    expect(reloadPage).not.toHaveBeenCalled()
  })

  /** A switch whose own request finds the session ended: apiFetch announces
   *  the expiry, then throws. */
  function sessionEndsDuringSwitch() {
    answers.switch = async () => {
      window.dispatchEvent(new CustomEvent('orb:session-expired'))
      throw new Error('Session expired')
    }
  }

  it('a visiting sign-in found ended by a switch that fails signs out', async () => {
    const { container } = await visitingShell()
    sessionEndsDuringSwitch()
    fireEvent.click(container.querySelector('#btn-switch-facility') as HTMLElement)
    fireEvent.click(await screen.findByRole('button', { name: /Quorvane General · Quorvane Medicine/ }))
    fireEvent.click(confirmButton())
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1))
    expect(reloadPage).toHaveBeenCalledTimes(1)
    expect(login).not.toHaveBeenCalled()
  })

  it('a visiting switch that is refused while the session lives changes nothing', async () => {
    const { container } = await visitingShell()
    answers.switch = async () => { throw new Error('Your account has no live visiting access at that facility.') }
    fireEvent.click(container.querySelector('#btn-switch-facility') as HTMLElement)
    fireEvent.click(await screen.findByRole('button', { name: /Quorvane General · Quorvane Medicine/ }))
    fireEvent.click(confirmButton())
    expect((await within(screen.getByRole('dialog')).findByRole('alert')).textContent)
      .toBe('Your account has no live visiting access at that facility.')
    expect(logout).not.toHaveBeenCalled()
    expect(reloadPage).not.toHaveBeenCalled()
    // The session still lives: a later expiry still signs out.
    act(() => { window.dispatchEvent(new CustomEvent('orb:session-expired')) })
    expect(logout).toHaveBeenCalledTimes(1)
    expect(reloadPage).toHaveBeenCalledTimes(1)
  })

  it('leaves a home sign-in found ended by a switch to the usual re-sign-in', async () => {
    sessionEndsDuringSwitch()
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = await shell()
    await waitFor(() => expect(container.querySelector('#btn-switch-facility')).toBeTruthy())
    fireEvent.click(container.querySelector('#btn-switch-facility') as HTMLElement)
    fireEvent.click(await screen.findByRole('button', { name: /Halvenor Satellite/ }))
    fireEvent.click(confirmButton())
    expect((await within(screen.getByRole('dialog')).findByRole('alert')).textContent).toBe('Session expired')
    expect(logout).not.toHaveBeenCalled()
    expect(reloadPage).not.toHaveBeenCalled()
  })
})
