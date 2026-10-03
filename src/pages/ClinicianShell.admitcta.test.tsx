/**
 * PERMISSION MIRRORING — the admit CTA.
 *
 * POST /api/admissions/admit requires `patient.write`
 * (server/routes/admission_routes.py), which only a doctor/clinician holds. The
 * sidebar gated the CTA on "not a pharmacist", so a nurse and an administrator
 * both got the shell's primary call-to-action on every screen, filled in a full
 * admission form, and were told 403 at the end of it — a button that existed
 * only to fail.
 *
 * The invariant asserted here is not "hidden for nurses". It is: the CTA renders
 * for exactly those principals whose server permission set contains
 * `patient.write`. A role list drifts; that is how this defect was born.
 */
import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

let signedIn: { id: string; name: string; role: string | null; department?: string } = {
  id: 'u1', name: 'Dr. Meera Sharma', role: 'doctor', department: 'General Medicine',
}

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
  getUser: () => signedIn,
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
  apiFetch: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../App', () => ({
  useAuth: () => ({ user: signedIn, logout: vi.fn() }),
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

// Chrome that is irrelevant to the CTA.
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

async function renderAs(role: string | null, department = 'General Medicine') {
  signedIn = { id: 'u1', name: 'Test User', role, department }
  const { container } = render(<ClinicianShell />)
  await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
  return container
}

describe('admit CTA mirrors POST /api/admissions/admit (patient.write)', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
    })))
  })

  // The server grants patient.write to doctor and its clinician alias, and to
  // nobody else (server/permissions.py:ROLE_PERMISSIONS).
  it.each(['doctor', 'clinician'])('renders for %s, who holds patient.write', async role => {
    const container = await renderAs(role)
    expect(container.querySelector('#btn-admit-patient')).toBeTruthy()
  })

  it.each([
    // A nurse charts observations, and is deliberately NOT granted the record
    // write — this is the defect's headline case.
    'nurse',
    // Governance + read-only clinical. Never patient.write.
    'admin',
    'pharmacist',
    // Raw, un-normalized role the shell keeps a nav safety net for. The server
    // has no such key, so it holds nothing and the admit POST would 403.
    'surgeon',
  ])('does not render for %s, who does not hold patient.write', async role => {
    const container = await renderAs(role)
    expect(container.querySelector('#btn-admit-patient')).toBeNull()
  })

  it('does not render when the session carries no role at all', async () => {
    const container = await renderAs(null)
    expect(container.querySelector('#btn-admit-patient')).toBeNull()
  })
})
