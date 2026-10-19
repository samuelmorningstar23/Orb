/**
 * The sidebar user chip and the facility (multisite-6, W9-H).
 *
 * Once more than one facility is on record, the chip names the facility the
 * account works at after the role and department, read fresh from
 * /api/auth/me. With one facility (every site today) the chip reads exactly
 * as before and nothing extra is fetched.
 */
import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const authState = vi.hoisted(() => ({
  user: { id: 'u1', name: 'Nina Nurse', role: 'nurse', department: 'Cardiology' as string | null } as Record<string, unknown>,
}))
const apiFetch = vi.hoisted(() => vi.fn())

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
  getUser: () => ({ id: 'u1', name: 'Nina Nurse', role: 'nurse' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
  getVitalsFeed: vi.fn(async () => ({ simulated: false })),
}))

vi.mock('../App', () => ({
  useAuth: () => ({ user: authState.user, logout: vi.fn() }),
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

// Chrome that is irrelevant to the user chip.
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

const meCalls = () => apiFetch.mock.calls.filter(c => c[0] === '/auth/me')

beforeEach(() => {
  markSwitchesKnownForTests()
  resetPlatformFlags()
  apiFetch.mockReset()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
  authState.user = { id: 'u1', name: 'Nina Nurse', role: 'nurse', department: 'Cardiology' }
})

afterEach(() => { resetPlatformFlags() })

async function chip(container: HTMLElement) {
  await waitFor(() => expect(container.querySelector('.shell__user-role')).toBeTruthy())
  return container.querySelector('.shell__user-role') as HTMLElement
}

describe('the user chip and the facility', () => {
  it('is unchanged with one facility, and fetches nothing for it', async () => {
    apiFetch.mockResolvedValue({ facility: { id: 1, code: 'MAIN', name: 'City Hospital' } })
    const { container } = render(<ClinicianShell />)
    const role = await chip(container)
    expect(role.textContent).toBe('role.nurse · Cardiology')
    expect(role.getAttribute('title')).toBe('role.nurse · Cardiology')
    expect(meCalls()).toHaveLength(0)
  })

  it('names the facility once more than one is on record', async () => {
    apiFetch.mockResolvedValue({ facility: { id: 2, code: 'SAT1', name: 'Riverside Satellite' } })
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    const { container } = render(<ClinicianShell />)
    const role = await chip(container)
    await waitFor(() => expect(role.textContent).toBe('role.nurse · Cardiology · Riverside Satellite'))
    expect(role.getAttribute('title')).toBe('role.nurse · Cardiology · Riverside Satellite')
    expect(meCalls()).toHaveLength(1)
  })

  it('follows the health answer when it lands after the shell opened', async () => {
    apiFetch.mockResolvedValue({ facility: { id: 2, code: 'SAT1', name: null } })
    authState.user = { id: 'u3', name: 'Pavan Pharm', role: 'pharmacist', department: null }
    const { container } = render(<ClinicianShell />)
    const role = await chip(container)
    expect(role.textContent).toBe('role.pharmacist')
    act(() => { setPlatformFlags({ multi_facility_data: true }) })
    // No stored name: the code stands in.
    await waitFor(() => expect(role.textContent).toBe('role.pharmacist · SAT1'))
  })

  it('falls back to the facility the sign-in carried when the check fails', async () => {
    apiFetch.mockRejectedValue(new Error('offline'))
    authState.user = { id: 'u2', name: 'Asha Admin', role: 'admin', department: null,
      facility: { id: 1, code: 'MAIN', name: 'City Hospital' } }
    setPlatformFlags({ multi_facility_data: true })
    const { container } = render(<ClinicianShell />)
    const role = await chip(container)
    await waitFor(() => expect(meCalls()).toHaveLength(1))
    expect(role.textContent).toBe('role.admin · City Hospital')
  })

  it('shows the role alone when the account has no facility on the list', async () => {
    apiFetch.mockResolvedValue({ facility: null })
    setPlatformFlags({ multi_facility_data: true })
    const { container } = render(<ClinicianShell />)
    const role = await chip(container)
    await waitFor(() => expect(meCalls()).toHaveLength(1))
    expect(role.textContent).toBe('role.nurse · Cardiology')
  })
})
