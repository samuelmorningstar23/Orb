/**
 * THE DOCTOR'S CHART HAD NO ALLERGY DATA ON THE CLIENT AT ALL.
 *
 * The nurse med round carries the allergy list on its patient card, so wiring
 * the Administer modal there was a matter of passing what was already in hand.
 * Vigil's chart — the doctor's route to the same modal, the same endpoint and
 * the same dose — held nothing. It never read `patient_allergies`.
 *
 * So this file pins THREE things about the read that was added:
 *
 *   1. Suresh Reddy's charted ceftriaxone anaphylaxis reaches the Administer
 *      modal opened from the chart, in full — allergen, severity, reaction.
 *   2. A FAILED read never renders as "no allergies". Those two states look
 *      identical and mean opposite things, and the fail-open version of this
 *      screen would tell a nurse a patient with a documented anaphylaxis had
 *      nothing on file.
 *   3. It cannot bleed across patients. An allergy list is the one thing on
 *      this chart where showing the previous patient's copy is worse than
 *      showing nothing: the nurse would be reassured, by name, about the wrong
 *      person.
 */
import { render, screen, act, waitFor, fireEvent, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { AdministrationRecord } from '../../api/client'
import { openPatient } from '../../lib/orbBus'

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  /** Resolves `/patients/{id}/allergies`, keyed by patient, with controllable
   *  timing so a mid-flight patient switch can be staged deliberately. */
  allergyFetch: vi.fn(),
}))

const CEFTRIAXONE_ORDER = {
  id: 811, drug_name: 'Ceftriaxone', dosage: '2 g', route: 'IV',
  frequency: 'OD', status: 'active', source: 'clinician', notes: null,
}

function detailFor(patientId: string, name: string, meds: unknown[] = [CEFTRIAXONE_ORDER]) {
  return {
    patient: {
      patient_id: patientId, name, bed: patientId === 'P104' ? 'GM-104' : 'GM-105',
      mrn: `MRN-${patientId}`, diagnosis: 'Community-acquired pneumonia',
      admission_date: '2026-08-12', age: 58, sex: 'M', comorbidities: [],
    },
    vitals: [{
      timestamp: '2026-08-14T09:00:00Z', heart_rate: 92, systolic_bp: 118,
      diastolic_bp: 74, respiratory_rate: 18, temperature: 37.6, spo2: 96,
    }],
    medications: meds, notes: [], reports: [], labs: [], alerts: [], lens_analyses: [],
    risk: { level: 'stable', score: 0.2, raw_news2: 1, drivers: [], breakdown: [], single_param_3: false },
    trend: { trend: 'stable', reason: null },
  }
}

const DETAILS: Record<string, ReturnType<typeof detailFor>> = {
  P104: detailFor('P104', 'Suresh Reddy'),
  P105: detailFor('P105', 'Anand Kumar'),
}

vi.mock('../../api/client', () => ({
  apiFetch: (path: string) => mocks.allergyFetch(path),
  getPatientDetail: vi.fn(async (p: string) => DETAILS[p]),
  getAdministrations: vi.fn(async () => []),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: AdministrationRecord | null | undefined) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  dischargePatient: vi.fn(),
  getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  addClinicalNote: vi.fn(),
  addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getDischargeQrObjectUrl: vi.fn(),
  prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  transitionSageAction: vi.fn(),
  getChannels: vi.fn(async () => []),
  getUser: () => ({ id: 'DOC1', name: 'Dr Iyer', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

vi.mock('../../components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('../../components/sage/SageAlertAssessment', () => ({ default: () => null }))
vi.mock('../../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import VigilPanel from './VigilPanel'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

/** His charted allergy, exactly as `patient_allergies` holds it. */
const SURESH_ALLERGY = {
  substance: 'Ceftriaxone',
  reaction: 'urticaria, angio-oedema, hypotension (2023)',
  severity: 'severe',
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.allergyFetch.mockResolvedValue({ allergies: [] })
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

async function open(patientId: string, name: string) {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient(patientId, name) })
  await screen.findByText(name)
}

/** Open the Administer modal the way a doctor does from the chart: the command
 *  bar's Medications button, then Administer on the ceftriaxone row. */
async function openAdministerModal() {
  const medsBtn = document.getElementById('btn-meds-reports')
  if (!medsBtn) throw new Error('no Medications button in the command bar')
  await act(async () => { fireEvent.click(medsBtn) })
  const btn = await screen.findByRole('button', { name: /^Administer$/ })
  await act(async () => { fireEvent.click(btn) })
  return await screen.findByRole('dialog', { name: /Administer medication/i })
}

describe('the chart reads the allergy list the Administer modal repeats', () => {
  it("asks for THIS patient's allergies when the chart opens", async () => {
    await open('P104', 'Suresh Reddy')
    await waitFor(() => expect(mocks.allergyFetch).toHaveBeenCalledWith('/patients/P104/allergies'))
  })

  it("carries Suresh Reddy's ceftriaxone anaphylaxis into the modal, in full", async () => {
    mocks.allergyFetch.mockResolvedValue({ allergies: [SURESH_ALLERGY] })
    await open('P104', 'Suresh Reddy')
    await waitFor(() => expect(mocks.allergyFetch).toHaveBeenCalled())
    const modal = await openAdministerModal()

    expect(within(modal).getByText(/Charted allergy to Ceftriaxone/i)).toBeInTheDocument()
    expect(within(modal).getByText('severe')).toBeInTheDocument()
    expect(within(modal).getByText(/urticaria, angio-oedema, hypotension \(2023\)/))
      .toBeInTheDocument()
  })

  it('renders a FAILED read as unavailable, never as "no allergies"', async () => {
    mocks.allergyFetch.mockRejectedValue(new Error('500'))
    await open('P104', 'Suresh Reddy')
    const modal = await openAdministerModal()

    expect(within(modal).getByText(/Allergy list unavailable/i)).toBeInTheDocument()
    expect(within(modal).queryByText(/NKDA/)).not.toBeInTheDocument()
  })

  it('renders a non-list response as unavailable too', async () => {
    // A shape change or a proxy error page must not read as an empty list.
    mocks.allergyFetch.mockResolvedValue({ detail: 'Patient not found' })
    await open('P104', 'Suresh Reddy')
    const modal = await openAdministerModal()
    expect(within(modal).getByText(/Allergy list unavailable/i)).toBeInTheDocument()
  })

  it('an empty list from the server is none recorded (never NKDA), not unavailable', async () => {
    mocks.allergyFetch.mockResolvedValue({ allergies: [] })
    await open('P104', 'Suresh Reddy')
    const modal = await openAdministerModal()
    expect(within(modal).getByText(/^No allergies recorded$/)).toBeInTheDocument()
    expect(within(modal).queryByText(/NKDA/)).not.toBeInTheDocument()
    expect(within(modal).queryByText(/Allergy list unavailable/i)).not.toBeInTheDocument()
  })
})

/* TWO DEFENCES HOLD THIS, and the case is written against the OUTCOME rather
   than either mechanism so it survives a rewrite of both: the chart view is
   keyed by patient (`key={selectedPatientId}`, so a switch remounts and state
   starts empty), and the allergy effect clears to `null` before it fetches and
   drops a late response through a `live` flag. Verified 2026-08-14 that the
   case fails when BOTH are removed — a test that only one of them satisfies
   would go quiet the moment the other was refactored away. */
describe('an allergy list never bleeds from the previous patient', () => {
  it("does not show Suresh Reddy's anaphylaxis on Anand Kumar's chart", async () => {
    // Suresh's allergy read is still in flight when the doctor switches charts;
    // it lands afterwards. Anand's read never returns at all. The honest state
    // for Anand is "unavailable" — under no circumstances Suresh's ceftriaxone.
    let resolveSuresh: (v: unknown) => void = () => {}
    mocks.allergyFetch.mockImplementation((path: string) => {
      if (path === '/patients/P104/allergies') {
        return new Promise(res => { resolveSuresh = res })
      }
      return new Promise(() => {})   // Anand's read hangs
    })

    await open('P104', 'Suresh Reddy')
    await act(async () => { openPatient('P105', 'Anand Kumar') })
    await screen.findByText('Anand Kumar')
    await act(async () => { resolveSuresh({ allergies: [SURESH_ALLERGY] }) })

    const modal = await openAdministerModal()
    expect(within(modal).queryByText('Ceftriaxone', { selector: '.administer-modal__allergen' }))
      .not.toBeInTheDocument()
    expect(within(modal).queryByText(/urticaria, angio-oedema/)).not.toBeInTheDocument()
    expect(within(modal).getByText(/Allergy list unavailable/i)).toBeInTheDocument()
  })
})
