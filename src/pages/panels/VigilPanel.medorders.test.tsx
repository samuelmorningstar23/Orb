/**
 * The chart's Medications window with the medication order store
 * (medorders-10, W9-D).
 *
 * With the server's 'med_order_store' feature off the window is the table it
 * always was. With it on the window lists the patient's orders
 * (components/medorders/MedOrderList, its own suite) and hands it the chart's
 * ward rows and administrations; the Add form is unchanged either way and
 * keeps posting to the chart's own route (ruling R010, held).
 */
import { render, screen, act, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openPatient } from '../../lib/orbBus'

const h = vi.hoisted(() => {
  const handlers: Record<string, Array<(p: unknown) => void>> = {}
  return {
    detail: null as unknown,
    board: [] as unknown[],
    handlers,
    getTimelineProjection: vi.fn(() => new Promise(() => {})),
    socket: {
      on: (ev: string, fn: (p: unknown) => void) => { (handlers[ev] ||= []).push(fn) },
      off: (ev: string, fn: (p: unknown) => void) => {
        handlers[ev] = (handlers[ev] || []).filter(f => f !== fn)
      },
      emit: () => {},
    },
  }
})

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({ allergies: [] })),
  getPatientDetail: vi.fn(async () => h.detail),
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: () => false,
  dischargePatient: vi.fn(),
  getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  addClinicalNote: vi.fn(),
  addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: h.getTimelineProjection,
  getDischargeQrObjectUrl: vi.fn(),
  prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  transitionSageAction: vi.fn(),
  getChannels: vi.fn(async () => []),
  getUser: () => ({ id: 'DOC1', name: 'Dr Iyer', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: h.socket,
}))

vi.mock('../../components/sage/SageInsightsFeed', () => ({ default: () => <div data-testid="insights-feed" /> }))
vi.mock('../../components/sage/WardBrief', () => ({ default: () => <div data-testid="ward-brief" /> }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => <div data-testid="care-bundle" /> }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => <div data-testid="course" /> }))


const listProps: Array<Record<string, unknown>> = []
vi.mock('../../components/medorders/MedOrderList', () => ({
  default: (props: Record<string, unknown>) => {
    listProps.push(props)
    return <div data-testid="med-order-list">Order list for {String(props.patientId)}</div>
  },
}))

import VigilPanel from './VigilPanel'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'
import { resetClinicalSwitches, resetFeatureFlags, setFeatureFlags } from '../../shellConfig'
import { addMedication } from '../../api/client'

const T = '2026-10-06T08:00:00Z'
const REASON = 'Scores are switched off on this computer.'
const PATIENT = {
  patient_id: 'P007', name: 'Rahul Menon', bed: '7', mrn: 'MRN-P007', ward: 'GM-1',
  diagnosis: 'Community-acquired pneumonia', admission_date: '2026-09-30',
  age: 71, sex: 'M', comorbidities: [],
}
const VITAL = {
  id: 501, patient_id: 'P007', timestamp: T,
  heart_rate: 135, systolic_bp: 85, diastolic_bp: 50, spo2: 88, temperature: 39.5, respiratory_rate: 30,
  consciousness: 'alert', on_oxygen: 0, source: 'clerk_transcribed',
}

function chartWithNoScore() {
  return {
    patient: PATIENT, vitals: [VITAL], medications: [], notes: [], scribe_notes: [],
    lab_observations: [], problems: [], lens_analyses: [], risk_history: [],
    risk: null, risk_off_reason: REASON, obs_schedule: null,
    trend: { trend: 'insufficient_data' }, simulated_vitals: false, site_name: null,
  }
}

async function openChart() {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient('P007', 'Rahul Menon') })
  await screen.findByText('Rahul Menon')
}


const AMOX = {
  id: 9, patient_id: 'P007', drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral',
  frequency: 'Q8H', status: 'active', source: 'clinician', unverified: false,
  started_at: '2026-10-05T08:00:00Z', notes: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  listProps.length = 0
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
  h.board = []
  h.detail = { ...chartWithNoScore(), medications: [AMOX], drug_chart: true }
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => h.board, headers: { get: () => String(h.board.length) },
  })))
  vi.stubGlobal('print', vi.fn())
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  resetFeatureFlags()
  vi.unstubAllGlobals()
})

async function openMeds() {
  await openChart()
  await act(async () => { document.getElementById('btn-meds-reports')!.click() })
  await screen.findByRole('dialog', { name: /Medications/ })
}

describe('the Medications window and the order store', () => {
  it('is the old table while the order store is off', async () => {
    await openMeds()
    expect(screen.getByRole('columnheader', { name: 'Administration' })).toBeTruthy()
    expect(screen.getByText('Amoxicillin')).toBeTruthy()
    expect(screen.queryByTestId('med-order-list')).toBeNull()
  })

  it('lists the orders with the chart rows handed in while it is on, and keeps the Add form', async () => {
    act(() => { setFeatureFlags({ med_order_store: true }) })
    await openMeds()
    expect(screen.getByTestId('med-order-list').textContent).toBe('Order list for P007')
    expect(screen.queryByRole('columnheader', { name: 'Administration' })).toBeNull()
    const props = listProps[listProps.length - 1]
    expect(props.chartRows).toEqual([AMOX])
    expect(props.canAdminister).toBe(true)
    expect(screen.getByTitle('Add Medication')).toBeTruthy()
  })

  it("hands the list the new ward row after the window's own Add form, with the window still open", async () => {
    // The list reloads its orders when the rows it is handed change
    // (MedOrderList's own suite); this pins that the chart hands them in.
    act(() => { setFeatureFlags({ med_order_store: true }) })
    await openMeds()
    const PARA = { ...AMOX, id: 10, drug_name: 'Paracetamol', dosage: '1 g', frequency: 'QDS' }
    vi.mocked(addMedication).mockImplementation(async () => {
      h.detail = { ...chartWithNoScore(), medications: [AMOX, PARA], drug_chart: true }
      return {} as Awaited<ReturnType<typeof addMedication>>
    })
    await act(async () => { screen.getByTitle('Add Medication').click() })
    const form = await screen.findByRole('dialog', { name: 'Add Medication' })
    fireEvent.change(within(form).getByLabelText('Drug Name'), { target: { value: 'Paracetamol' } })
    fireEvent.change(within(form).getByLabelText('Dose'), { target: { value: '1 g' } })
    fireEvent.change(within(form).getByLabelText('Frequency'), { target: { value: 'QDS' } })
    await act(async () => { within(form).getByRole('button', { name: 'Add Medication' }).click() })

    expect(addMedication).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(listProps[listProps.length - 1].chartRows).toEqual([AMOX, PARA]))
    expect(screen.getByRole('dialog', { name: /Medications/ })).toBeTruthy()
    expect(screen.getByTestId('med-order-list')).toBeTruthy()
  })
})
