/**
 * THE DOCTOR'S ROUTE TO THE PILOT'S GROUND TRUTH.
 *
 * A doctor holds OUTCOME_RECORD (server/permissions.py) — the site that names
 * the ward doctor, not the charge nurse, as the shift's one recorder depends on
 * it. But the capture screen lived only inside the nurse panel, which is not in
 * a doctor's tab list: a granted permission with no way to exercise it, and a
 * ward-shift silently counted as uncovered time.
 *
 * Three properties, all of them clinical rather than cosmetic:
 *
 *   1. the entry point is gated on the PERMISSION mirror, not on a role list —
 *      it renders for the roles the server grants `outcome.record` and for no
 *      others;
 *   2. it opens the SAME OutcomeCaptureSheet the nurse uses (composition, not a
 *      copy), so the two recorders cannot drift into two ground truths;
 *   3. THE BLINDING HOLDS. A recorder who can watch the scorecard move as they
 *      enter outcomes has been handed an incentive to enter them differently,
 *      so the capture path must never read the scorecard — no request to the
 *      admin read surface, and no rate or metric on screen.
 */
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const WARD = 'General Medicine'

/** Every API path the capture surface touches, in order. */
const apiCalls: string[] = []
/** Raw fetches (the ward board's paginated load). */
const rawFetches: string[] = []

const shiftStatus = {
  generated_at: '2026-08-11T14:00:00.000+00:00',
  status_available: true,
  shifts: [{
    shift: 'morning',
    period_start: '2026-08-11T07:00:00.000+00:00',
    period_end: '2026-08-11T15:00:00.000+00:00',
    label: '07:00–15:00',
    is_current: true,
    wards: [{
      ward: WARD, confirmed: false, confirmed_at: null, confirmed_by_role: null,
      events_recorded: 0, status: 'outstanding',
    }],
  }],
  max_backdate_hours: 72,
  event_types: ['icu_transfer', 'code_blue'],
  sources: ['witnessed', 'chart_review', 'handover'],
  recognition: ['clinician_independent', 'prompted_by_orb', 'unknown'],
  why: 'A ward-shift with neither a recorded event nor a confirmation is uncovered time.',
}

const apiFetch = vi.fn(async (path: string) => {
  apiCalls.push(path)
  if (path.includes('/outcome/shift-status')) return shiftStatus
  return {}
})

vi.mock('../../api/client', () => ({
  apiFetch: (p: string) => apiFetch(p),
  getPatientDetail: vi.fn(async () => null),
  getAdministrations: vi.fn(async () => []),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: any) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  dischargePatient: vi.fn(), getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  addClinicalNote: vi.fn(), addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getDischargeQrObjectUrl: vi.fn(), prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  getUser: () => ({ id: 'DOC001', name: 'Dr Iyer', role: 'doctor', department: WARD }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

vi.mock('../../components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('../../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import VigilPanel from './VigilPanel'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

const BOARD = [{
  id: 'P0012', name: 'Anita Rao', mrn: 'MRN-12', bed: '4', ward: WARD,
  admission_date: '2026-08-05', diagnosis: 'Sepsis', age: 61, sex: 'F',
  risk_score: 0.2, risk_level: 'stable', risk_drivers: [],
}]

function renderPanel(role: string | null) {
  return render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role={role} />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
}

/** Renders the board and waits for its first load to settle. */
async function boardFor(role: string | null) {
  const view = renderPanel(role)
  await screen.findByText('Anita Rao')
  return view
}

beforeEach(() => {
  apiCalls.length = 0
  rawFetches.length = 0
  apiFetch.mockClear()
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    rawFetches.push(String(url))
    return { ok: true, status: 200, json: async () => BOARD, headers: { get: () => '1' } }
  }))
})

describe('doctor entry point to end-of-shift outcome capture', () => {
  // Roles the server grants OUTCOME_RECORD. The doctor is the one this change
  // exists for; the others already had a route and must keep one here too.
  it.each(['doctor', 'clinician', 'nurse', 'admin'])(
    'renders for %s, who holds outcome.record', async role => {
      await boardFor(role)
      expect(screen.getByRole('button', { name: /shift outcomes/i })).toBeTruthy()
    })

  it.each(['pharmacist', 'patient', 'surgeon'])(
    'does not render for %s, who does not hold outcome.record', async role => {
      await boardFor(role)
      expect(screen.queryByRole('button', { name: /shift outcomes/i })).toBeNull()
    })

  it('does not render when the session carries no role', async () => {
    await boardFor(null)
    expect(screen.queryByRole('button', { name: /shift outcomes/i })).toBeNull()
  })

  it('opens the same OutcomeCaptureSheet the nurse uses, prefilled and live', async () => {
    await boardFor('doctor')
    fireEvent.click(screen.getByRole('button', { name: /shift outcomes/i }))

    const dialog = await screen.findByRole('dialog', { name: /end-of-shift outcomes/i })

    // The sheet itself, not a lookalike: its ward field, its shift window, and
    // its two writes.
    const wardField = dialog.querySelector('#shift-capture-ward') as HTMLInputElement
    expect(wardField).toBeTruthy()
    // Prefilled from the signed-in clinician's department, exactly as in the
    // nurse panel — the doctor does not retype their own ward.
    expect(wardField.value).toBe(WARD)

    await waitFor(() =>
      expect(apiCalls.some(p => p.includes('/outcome/shift-status'))).toBe(true))
    await screen.findByRole('button', { name: /no deterioration events this shift/i })
    expect(screen.getByRole('button', { name: /record a deterioration event/i })).toBeTruthy()
  })

  it('feeds the board patients into the picker without reshaping the sheet', async () => {
    await boardFor('doctor')
    fireEvent.click(screen.getByRole('button', { name: /shift outcomes/i }))
    const dialog = await screen.findByRole('dialog', { name: /end-of-shift outcomes/i })

    fireEvent.click(await screen.findByRole('button', { name: /record a deterioration event/i }))
    const option = dialog.querySelector('#outcome-patient-options option') as HTMLOptionElement
    // Vigil rows carry `id`; the picker's contract is `patient_id`.
    expect(option.value).toBe('P0012')
    expect(option.textContent).toBe('Anita Rao')
  })

  it('BLINDING: the capture path never reads the scorecard', async () => {
    await boardFor('doctor')
    fireEvent.click(screen.getByRole('button', { name: /shift outcomes/i }))
    await screen.findByRole('dialog', { name: /end-of-shift outcomes/i })
    await waitFor(() =>
      expect(apiCalls.some(p => p.includes('/outcome/shift-status'))).toBe(true))
    // Open the event form too — every control on the capture surface.
    fireEvent.click(await screen.findByRole('button', { name: /record a deterioration event/i }))
    await act(async () => { await Promise.resolve() })

    // No request to the admin read surface, by either transport.
    for (const path of [...apiCalls, ...rawFetches]) {
      expect(path).not.toMatch(/scorecard|adjudicat|export/i)
    }
    // And the shadow calls it DID make are only the capture endpoints.
    const shadow = apiCalls.filter(p => p.includes('/admin/shadow'))
    expect(shadow.length).toBeGreaterThan(0)
    for (const path of shadow) {
      expect(path.startsWith('/admin/shadow/outcome')).toBe(true)
    }
  })
})
