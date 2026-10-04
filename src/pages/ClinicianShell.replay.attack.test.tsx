/**
 * ATTACK: the shell's open-patient REPLAY can re-open the previous patient
 * after the clinician has already moved to the next one.
 *
 * ClinicianShell.tsx:326-334 re-dispatches every `orb:open-patient` event 200ms
 * later (so a freshly-mounted VigilPanel catches the very first jump), guarded
 * by a single shared `replaying` boolean. Two jumps inside that 200ms window
 * interleave: the replay of the FIRST patient fires after the SECOND patient is
 * already open, and VigilPanel's listener (VigilPanel.tsx:91) takes it.
 *
 * This is independent of the PatientDetailView `key` fix — the key makes the
 * swap a clean remount, but a clean remount of the WRONG patient is still the
 * wrong chart on screen.
 */
import { render, screen, act, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

const detailFor = (id: string, name: string, bed: string) => ({
  patient: {
    patient_id: id, name, bed, mrn: `MRN-${id}`, diagnosis: 'Sepsis',
    admission_date: '2026-08-01', age: 61, sex: 'M', comorbidities: [],
  },
  vitals: [], medications: [], notes: [], reports: [], labs: [], alerts: [], lens_analyses: [],
  risk: { level: 'stable', score: 0.1, raw_news2: 1, drivers: [], breakdown: [], single_param_3: false },
  trend: { trend: 'stable' },
})
const DETAIL: Record<string, ReturnType<typeof detailFor>> = {
  'PAT-A': detailFor('PAT-A', 'Anita Rao', '3'),
  'PAT-B': detailFor('PAT-B', 'Rahul Menon', '7'),
}

vi.mock('../api/client', () => ({
  getPatientDetail: vi.fn(async (pid: string) => DETAIL[pid]),
  getAdministrations: vi.fn(async () => []),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  dischargePatient: vi.fn(), addClinicalNote: vi.fn(), addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getDischargeQrObjectUrl: vi.fn(), prefetchSageContext: vi.fn(async () => {}),
  // Ward-board "N new since you last looked" badge. Resolves to {} so the
  // board renders no badges — the correct degraded state, and the one this
  // file's assertions about cross-patient bleed are indifferent to.
  getUnseenCounts: vi.fn(async () => ({})),
  // The chart reads the patient's charted allergies so the Administer modal can
  // repeat them at the moment of administration (2026-08-15). Rejecting here is
  // the honest stub for this file: it asserts cross-patient bleed, and an
  // unreadable allergy list renders as "not known" rather than as "no
  // allergies" — the safe state — which keeps this test indifferent to it.
  apiFetch: vi.fn(async () => { throw new Error('not used in this test'); }),
  administerMedication: vi.fn(),
  getUser: () => ({ id: 'u1', name: 'Dr Iyer', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  // Nurse mode's Deliveries tab (src/pages/panels/nurse/DeliveriesSheet.tsx)
  // imports these two. Every panel is now its own lazy chunk
  // (ClinicianShell.tsx `lazy(() => import(...))`), and this test opens Vigil
  // as a doctor, so NursePanel's chunk never loads here: deleting both entries
  // leaves the test green (checked 2026-10-03). They are kept so a step that
  // opens the nurse shift tab does not trip over a missing mock export. This
  // file mocks the whole client with a factory, so any NAMED export a loaded
  // panel imports must be listed here.
  getOrdersAwaitingReceipt: vi.fn(async () => ({ orders: [] })),
  receiveHelixOrder: vi.fn(async () => ({ ok: true })),
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'u1', name: 'Dr Iyer', role: 'doctor', department: 'ICU' }, logout: vi.fn() }),
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

// Chrome that is irrelevant to the replay path.
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

describe('ClinicianShell open-patient replay', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
    })))
  })
  afterEach(() => { vi.useRealTimers() })

  it('does not re-open the previous patient after the clinician has jumped to the next one', async () => {
    render(<ClinicianShell />)
    /* Open Patients through the shell's own navigation bus rather than relying
       on it being the landing tab. A doctor now lands on the composed home
       ("Today") when that panel is installed, and panels are mount-on-first-
       view, so #panel-vigil does not exist until something navigates to it.
       This is the same door the command palette and every deep link use, so the
       replay window under attack is entered exactly as it is in production —
       and the test no longer asserts a landing-tab decision it does not own. */
    await act(async () => {
      window.dispatchEvent(new CustomEvent('orb:goto-tab', { detail: { tab: 'vigil' } }))
    })
    // Wait for the lazily-loaded Vigil panel to mount its listener. The wait is
    // long on purpose: on a cold run the lazy VigilPanel graph takes ~3.5 s to
    // transform (measured 2026-10-03), and the 1 s default failed this test on
    // compile time alone. A real link failure (the mock note above) never
    // mounts at all, so it still goes red here, just later.
    await waitFor(() => expect(document.querySelector('#panel-vigil')).toBeTruthy(), { timeout: 10000 })
    await act(async () => { await Promise.resolve() })

    // Record every open-patient event that reaches the window, with timing, so
    // a green result cannot be a false negative from a missed interleave.
    const seen: { id: string; t: number }[] = []
    const t0 = Date.now()
    window.addEventListener('orb:open-patient', (e: Event) => {
      seen.push({ id: (e as CustomEvent).detail?.patientId, t: Date.now() - t0 })
    })

    // Jump 1 then jump 2, back to back, well inside the shell's 200ms window.
    await act(async () => {
      window.dispatchEvent(new CustomEvent('orb:open-patient', { detail: { patientId: 'PAT-A' } }))
      await Promise.resolve()
      window.dispatchEvent(new CustomEvent('orb:open-patient', { detail: { patientId: 'PAT-B' } }))
      await Promise.resolve()
    })
    await screen.findByText('Rahul Menon')

    // Let every scheduled replay fire.
    await act(async () => {
      await new Promise(r => setTimeout(r, 800))
    })
    /* EXECUTED RESULT: seen = A@0ms, B@0ms, A@201ms, B@202ms. The interleave is
       real — VigilPanel does receive a replayed open-patient for the PREVIOUS
       patient after the next one is open — but the replays preserve dispatch
       order, so the last event is always the last patient the clinician chose
       and the chart self-corrects. The exposure is a flicker bounded by the gap
       between the two clicks (<200ms), not a persistently wrong chart.
       This assertion exists so a future change to the replay guard that leaves
       the previous patient on screen fails here. */
    expect(seen.length).toBeGreaterThanOrEqual(3)

    // The chart the doctor is looking at must still be the one they last opened.
    expect(screen.queryByText('Anita Rao')).toBeNull()
    expect(screen.getByText('Rahul Menon')).toBeInTheDocument()
  })
})
