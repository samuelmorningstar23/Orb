/**
 * ATTACK: cross-patient bleed in the Vigil chart when the open patient changes
 * while requests are in flight.
 *
 * PatientDetailView is NOT keyed by patientId, so an `orb:open-patient` event
 * (Sage's ward-brief watchlist — the fastest route to the sickest patients, so
 * exactly the list a doctor clicks twice in a row) swaps the prop under a LIVE
 * component instead of remounting it. Every fetch it owns then races, and none
 * of them check whether the patient they were issued for is still the open one.
 *
 * Two surfaces are asserted here:
 *   1. the chart itself (command bar identity), and
 *   2. the eMAR "Given HH:MM" chip, which is what tells a nurse a dose has
 *      already been given.
 */
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { openPatient } from '../../lib/orbBus'

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void }
function defer<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

const detailFor = (id: string, name: string, bed: string, medId: number, drug: string) => ({
  patient: {
    patient_id: id, name, bed, mrn: `MRN-${id}`, diagnosis: 'Sepsis',
    admission_date: '2026-08-01', age: 61, sex: 'M', comorbidities: [],
  },
  vitals: [],
  medications: [{ id: medId, drug_name: drug, dosage: '1 g', route: 'IV', frequency: 'BD', status: 'active' }],
  notes: [], reports: [], labs: [], alerts: [], lens_analyses: [],
  risk: { level: 'stable', score: 0.1, raw_news2: 1, drivers: [], breakdown: [], single_param_3: false },
  trend: { trend: 'stable' },
})

const DETAIL: Record<string, any> = {
  'PAT-A': detailFor('PAT-A', 'Anita Rao', '3', 101, 'Vancomycin'),
  'PAT-B': detailFor('PAT-B', 'Rahul Menon', '7', 202, 'Ceftriaxone'),
}

const pendingDetail: Record<string, Deferred<any>> = {}
const pendingAdmin: Record<string, Deferred<any[]>> = {}

const getPatientDetail = vi.fn((pid: string) => {
  pendingDetail[pid] = defer<any>()
  return pendingDetail[pid].promise
})
const getAdministrations = vi.fn((pid: string) => {
  pendingAdmin[pid] = defer<any[]>()
  return pendingAdmin[pid].promise
})

vi.mock('../../api/client', () => ({
  // The chart's raw-path reads (house census on the board, the charted allergy
  // list the Administer modal repeats). Resolves empty so this file's timing
  // assertions are unaffected — the allergy list's own mid-flight behaviour is
  // pinned in VigilPanel.allergy.test.tsx.
  apiFetch: vi.fn(async () => ({ allergies: [] })),
  getPatientDetail: (p: string) => getPatientDetail(p),
  getAdministrations: (p: string) => getAdministrations(p),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: any) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  dischargePatient: vi.fn(), getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  addClinicalNote: vi.fn(), addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getDischargeQrObjectUrl: vi.fn(), prefetchSageContext: vi.fn(async () => {}),
  // Ward-board "N new since you last looked" badge. Resolves to {} so the
  // board renders no badges — the correct degraded state, and the one this
  // file's assertions about cross-patient bleed are indifferent to.
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  getUser: () => ({ id: 'u1', name: 'Dr Iyer', role: 'doctor' }),
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

function renderPanel() {
  return render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
}

describe('Vigil chart when the open patient changes mid-flight', () => {
  beforeEach(() => {
    getAdministrations.mockClear()
    getPatientDetail.mockClear()
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [],
      headers: { get: () => '0' },
    })))
  })

  it('a late response for the previous patient never lands in the open chart', async () => {
    renderPanel()

    // Doctor opens the first watchlist patient…
    await act(async () => { openPatient('PAT-A', 'Anita Rao') })
    await waitFor(() => expect(pendingDetail['PAT-A']).toBeTruthy())

    // …then, before it paints, the next one down the list.
    await act(async () => { openPatient('PAT-B', 'Rahul Menon') })
    await waitFor(() => expect(pendingDetail['PAT-B']).toBeTruthy())

    // Patient B answers first and is what the doctor is now looking at.
    await act(async () => {
      pendingDetail['PAT-B'].resolve(DETAIL['PAT-B'])
      pendingAdmin['PAT-B'].resolve([])
      await Promise.resolve()
    })
    await screen.findByText('Rahul Menon')

    // Now patient A's slow responses finally land.
    await act(async () => {
      pendingDetail['PAT-A'].resolve(DETAIL['PAT-A'])
      pendingAdmin['PAT-A'].resolve([{
        id: 9, medication_id: 202, patient_id: 'PAT-A', drug_name: 'Vancomycin',
        status: 'given', administered_at: '2026-08-06T09:00:00Z',
        administered_by: 'u2', administered_by_name: 'NURSE FROM OTHER PATIENT',
      }])
      await Promise.resolve()
    })

    // 1. The chart must still be patient B's.
    expect(screen.queryByText('Anita Rao')).toBeNull()
    expect(screen.getByText('Rahul Menon')).toBeInTheDocument()

    // 2. And the eMAR history behind the Medications modal must be B's.
    fireEvent.click(document.getElementById('btn-meds-reports') as HTMLElement)
    expect(screen.queryByTitle(/NURSE FROM OTHER PATIENT/)).toBeNull()
  })
})
