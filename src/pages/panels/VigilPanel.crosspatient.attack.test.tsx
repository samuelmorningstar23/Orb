/**
 * ATTACK (independent verification): cross-patient bleed in the Vigil chart on a
 * mid-flight patient switch, on the two surfaces the sequence-stamp guards in
 * loadPatientData/loadAdministrations do NOT cover.
 *
 * The realistic route is NOT a double-click on the ward-brief watchlist (that
 * list is only rendered on the ward board, and the first click replaces it with
 * the chart). It is the command palette — CommandPalette.tsx:127 dispatches
 * `orb:open-patient` and the palette is mounted shell-wide
 * (ClinicianShell.tsx:728), so it is reachable from INSIDE an open chart. Two
 * Cmd+K jumps, or one jump while the first chart is still loading, swap
 * patientId under a live PatientDetailView.
 *
 * 1. Clinical course: loadProjection() (VigilPanel.tsx:508) has no sequence
 *    guard. Only the `key` on PatientDetailView stops patient A's expected
 *    course from painting under patient B's name.
 * 2. Add Medication: the modal's open flag and typed drug/dose/frequency are
 *    component state. Without a remount they survive the switch and
 *    submitMedication() posts them against the NEW patientId
 *    (VigilPanel.tsx:659) — a wrong-patient drug order.
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

const detailFor = (id: string, name: string, bed: string) => ({
  patient: {
    patient_id: id, name, bed, mrn: `MRN-${id}`, diagnosis: 'Sepsis',
    admission_date: '2026-08-01', age: 61, sex: 'M', comorbidities: [],
  },
  vitals: [],
  medications: [],
  notes: [], reports: [], labs: [], alerts: [], lens_analyses: [],
  risk: { level: 'stable', score: 0.1, raw_news2: 1, drivers: [], breakdown: [], single_param_3: false },
  trend: { trend: 'stable' },
})

const DETAIL: Record<string, any> = {
  'PAT-A': detailFor('PAT-A', 'Anita Rao', '3'),
  'PAT-B': detailFor('PAT-B', 'Rahul Menon', '7'),
}

// Two clinically opposite courses. If A's lands under B's name, a doctor reads
// "comfort care, 40-day stay" for a patient who is on a weaning pathway.
const PROJECTION: Record<string, any> = {
  'PAT-A': {
    expected_los_days: 40, source: 'llm', current_phase_index: 0,
    phases: [{ name: 'COMFORT CARE PHASE A', focus: 'symptom control', typical_days: 'days 1-40' }],
  },
  'PAT-B': {
    expected_los_days: 4, source: 'llm', current_phase_index: 0,
    phases: [{ name: 'VENT WEAN PHASE B', focus: 'daily SBT', typical_days: 'days 1-4' }],
  },
}

const pendingDetail: Record<string, Deferred<any>> = {}
const pendingProjection: Record<string, Deferred<any>> = {}

const getPatientDetail = vi.fn((pid: string) => {
  pendingDetail[pid] = defer<any>()
  return pendingDetail[pid].promise
})
const getTimelineProjection = vi.fn((pid: string) => {
  pendingProjection[pid] = defer<any>()
  return pendingProjection[pid].promise
})
const addMedication = vi.fn(
  async (_patientId: string, _med: any, _ack?: any) => ({ safety: undefined }),
)

vi.mock('../../api/client', () => ({
  // The chart's raw-path reads (house census on the board, the charted allergy
  // list the Administer modal repeats). Resolves empty here; the allergy list's
  // own cross-patient behaviour is pinned in VigilPanel.allergy.test.tsx.
  apiFetch: vi.fn(async () => ({ allergies: [] })),
  getPatientDetail: (p: string) => getPatientDetail(p),
  getAdministrations: vi.fn(async () => []),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: any) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  getTimelineProjection: (p: string) => getTimelineProjection(p),
  dischargePatient: vi.fn(), getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  addClinicalNote: vi.fn(),
  addMedication: (pid: string, med: any, ack?: any) => addMedication(pid, med, ack),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
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
// ClinicalCourseTimeline is deliberately NOT mocked — the assertion below reads
// the real rendered course.

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

describe('Vigil chart: cross-patient bleed on a mid-flight patient switch', () => {
  beforeEach(() => {
    getPatientDetail.mockClear()
    getTimelineProjection.mockClear()
    addMedication.mockClear()
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [],
      headers: { get: () => '0' },
    })))
  })

  it("a late clinical-course projection for the previous patient never paints in the open chart", async () => {
    renderPanel()

    await act(async () => { openPatient('PAT-A', 'Anita Rao') })
    await waitFor(() => expect(pendingProjection['PAT-A']).toBeTruthy())

    // Second command-palette jump before the first chart has painted.
    await act(async () => { openPatient('PAT-B', 'Rahul Menon') })
    await waitFor(() => expect(pendingProjection['PAT-B']).toBeTruthy())

    await act(async () => {
      pendingDetail['PAT-B'].resolve(DETAIL['PAT-B'])
      pendingProjection['PAT-B'].resolve(PROJECTION['PAT-B'])
      await Promise.resolve()
    })
    await screen.findByText('Rahul Menon')
    expect(screen.getByText('VENT WEAN PHASE B')).toBeInTheDocument()

    // Patient A's slow projection finally lands.
    await act(async () => {
      pendingDetail['PAT-A'].resolve(DETAIL['PAT-A'])
      pendingProjection['PAT-A'].resolve(PROJECTION['PAT-A'])
      await Promise.resolve()
    })

    expect(screen.getByText('Rahul Menon')).toBeInTheDocument()
    expect(screen.queryByText('COMFORT CARE PHASE A')).toBeNull()
    expect(screen.queryByText('~40-day expected stay')).toBeNull()
    expect(screen.getByText('VENT WEAN PHASE B')).toBeInTheDocument()
  })

  it('an Add Medication form opened for one patient cannot be submitted against the next', async () => {
    renderPanel()

    await act(async () => { openPatient('PAT-A', 'Anita Rao') })
    await waitFor(() => expect(pendingDetail['PAT-A']).toBeTruthy())
    await act(async () => {
      pendingDetail['PAT-A'].resolve(DETAIL['PAT-A'])
      await Promise.resolve()
    })
    await screen.findByText('Anita Rao')

    // Doctor starts an order on Anita Rao's chart (Medications modal → "+").
    fireEvent.click(document.getElementById('btn-meds-reports') as HTMLElement)
    fireEvent.click(await screen.findByTitle('Add Medication'))
    const dialog = await screen.findByRole('dialog', { name: 'Add Medication' })
    const [drug, dose] = Array.from(dialog.querySelectorAll('input')) as HTMLInputElement[]
    const freq = (dialog.querySelectorAll('input')[2] as HTMLInputElement)
    fireEvent.change(drug, { target: { value: 'Vancomycin' } })
    fireEvent.change(dose, { target: { value: '1 g' } })
    fireEvent.change(freq, { target: { value: 'BD' } })

    // …and is pulled to another patient mid-order (command palette / Sage jump).
    await act(async () => { openPatient('PAT-B', 'Rahul Menon') })
    await waitFor(() => expect(pendingDetail['PAT-B']).toBeTruthy())
    await act(async () => {
      pendingDetail['PAT-B'].resolve(DETAIL['PAT-B'])
      await Promise.resolve()
    })
    await screen.findByText('Rahul Menon')

    // If the half-typed order survived the switch, the doctor's next click on
    // the confirm button is what actually orders the drug. Press it.
    const stillOpen = screen.queryByRole('dialog', { name: 'Add Medication' })
    if (stillOpen) {
      const confirm = stillOpen.querySelector('button.modal__confirm') as HTMLButtonElement
      fireEvent.click(confirm)
      await waitFor(() => expect(addMedication).toHaveBeenCalled())
    }

    // Vancomycin was typed for Anita Rao. It must never be posted for Rahul Menon.
    expect(addMedication.mock.calls.map((c: any[]) => [c[0], c[1]?.drug_name]))
      .not.toContainEqual(['PAT-B', 'Vancomycin'])

    // And the half-typed order for A must not be sitting open over B's chart.
    expect(screen.queryByRole('dialog', { name: 'Add Medication' })).toBeNull()
  })

  /* HAPPY PATH — the fix remounts the chart on a patient switch. That must not
     cost the clinician anything on the ordinary single-patient flow, and must
     not fire on a repeat open of the SAME patient (the shell replays every
     open-patient event 200ms later with the same id — if that remounted, it
     would wipe a half-typed order 200ms after the doctor started it). */
  it('HAPPY PATH: an order typed and submitted on one chart posts for that patient', async () => {
    renderPanel()

    await act(async () => { openPatient('PAT-A', 'Anita Rao') })
    await waitFor(() => expect(pendingDetail['PAT-A']).toBeTruthy())
    await act(async () => {
      pendingDetail['PAT-A'].resolve(DETAIL['PAT-A'])
      await Promise.resolve()
    })
    await screen.findByText('Anita Rao')

    fireEvent.click(document.getElementById('btn-meds-reports') as HTMLElement)
    fireEvent.click(await screen.findByTitle('Add Medication'))
    const dialog = await screen.findByRole('dialog', { name: 'Add Medication' })
    const inputs = Array.from(dialog.querySelectorAll('input')) as HTMLInputElement[]
    fireEvent.change(inputs[0], { target: { value: 'Vancomycin' } })
    fireEvent.change(inputs[1], { target: { value: '1 g' } })
    fireEvent.change(inputs[2], { target: { value: 'BD' } })

    await act(async () => {
      fireEvent.click(dialog.querySelector('button.modal__confirm') as HTMLButtonElement)
      await Promise.resolve()
    })

    await waitFor(() => expect(addMedication).toHaveBeenCalled())
    expect(addMedication.mock.calls[0][0]).toBe('PAT-A')
    expect(addMedication.mock.calls[0][1].drug_name).toBe('Vancomycin')
    // The order went through and the form closed — no lost work.
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Add Medication' })).toBeNull())

    // A successful order refreshes the chart; let that refresh land.
    await act(async () => {
      pendingDetail['PAT-A'].resolve(DETAIL['PAT-A'])
      await Promise.resolve()
    })
    // Still on the same chart, still the same patient.
    expect(await screen.findByText('Anita Rao')).toBeInTheDocument()
  })

  it('HAPPY PATH: re-opening the SAME patient does not remount and does not discard in-progress work', async () => {
    renderPanel()

    await act(async () => { openPatient('PAT-A', 'Anita Rao') })
    await waitFor(() => expect(pendingDetail['PAT-A']).toBeTruthy())
    await act(async () => {
      pendingDetail['PAT-A'].resolve(DETAIL['PAT-A'])
      await Promise.resolve()
    })
    await screen.findByText('Anita Rao')

    fireEvent.click(document.getElementById('btn-meds-reports') as HTMLElement)
    fireEvent.click(await screen.findByTitle('Add Medication'))
    const dialog = await screen.findByRole('dialog', { name: 'Add Medication' })
    const drug = dialog.querySelectorAll('input')[0] as HTMLInputElement
    fireEvent.change(drug, { target: { value: 'Vancomycin' } })

    const fetchesBefore = getPatientDetail.mock.calls.length

    // The shell's 200ms replay re-dispatches the SAME patient id.
    await act(async () => { openPatient('PAT-A', 'Anita Rao') })
    await act(async () => { await Promise.resolve() })

    // No remount: no refetch, modal still open, typed text intact.
    expect(getPatientDetail.mock.calls.length).toBe(fetchesBefore)
    const stillThere = screen.getByRole('dialog', { name: 'Add Medication' })
    expect((stillThere.querySelectorAll('input')[0] as HTMLInputElement).value).toBe('Vancomycin')
  })
})
