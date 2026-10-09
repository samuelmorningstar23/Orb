/**
 * The sidebar user chip for an account with no department, and for a cashier.
 *
 * A hospital-wide account (an administrator, the central pharmacy) carries no
 * department, and the chip rendered "Administrator ·" with the tooltip
 * "Administrator · null". A cashier's chip read "Staff" because the role list
 * stopped at pharmacist.
 */
import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const authState = vi.hoisted(() => ({
  user: { id: 'u1', name: 'Asha Admin', role: 'admin', department: null as string | null },
}))

vi.mock('../api/client', () => ({
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
  getUser: () => ({ id: 'u1', name: 'Dr. Meera Sharma-Venkataraman', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
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
import { markSwitchesKnownForTests } from '../shellConfig'

// These screens render before any health probe answers. The switches count
// as known, as on a page the server served; the startup rule itself is
// tested in ClinicianShell.startup.test.tsx.
beforeEach(() => { markSwitchesKnownForTests() })

describe('ClinicianShell user chip without a department', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
    })))
  })

  it('shows the role alone, with no trailing separator or "null"', async () => {
    authState.user = { id: 'u1', name: 'Asha Admin', role: 'admin', department: null }
    const { container } = render(<ClinicianShell />)
    await waitFor(() => expect(container.querySelector('.shell__user-role')).toBeTruthy())
    const role = container.querySelector('.shell__user-role')!
    expect(role.textContent).toBe('role.admin')
    expect(role.getAttribute('title')).toBe('role.admin')
  })

  it('labels a cashier as a cashier, not as staff', async () => {
    authState.user = { id: 'u2', name: 'Kiran Cash', role: 'cashier', department: 'Billing' }
    const { container } = render(<ClinicianShell />)
    await waitFor(() => expect(container.querySelector('.shell__user-role')).toBeTruthy())
    expect(container.querySelector('.shell__user-role')!.textContent).toBe('role.cashier · Billing')
  })
})
