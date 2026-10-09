/**
 * The Vigil chart hides the drug lists on an Orb Ward Record computer
 * (Samuel's ruling, 9 October 2026: "hide the drug lists"). The drug chart
 * stays on paper there. Under the profile the chart draws no Medications
 * button, no medicines list or Add Medication form, no Print medications and
 * no medicines section on Print record, and says nothing about medicines at
 * all. The chart answer says so itself (drug_chart false), so this holds while
 * the health probe has not answered yet. With the profile off the chart is as
 * it was.
 */
import { render, screen, act, waitFor, cleanup } from '@testing-library/react'
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

import VigilPanel from './VigilPanel'
import { dischargePatient, getAdministrations, getPatientForecast, prefetchSageContext } from '../../api/client'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'

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


const MEDICINE_WORDS = /medic|\bdrugs?\b|\bmeds?\b|Amoxicillin/i
const AMOX = {
  id: 9, patient_id: 'P007', drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral',
  frequency: 'Q8H', status: 'active', source: 'clinician', unverified: false,
  started_at: '2026-10-05T08:00:00Z', notes: null,
}

function withDrugs(drugChart: boolean | undefined) {
  const d: Record<string, unknown> = { ...chartWithNoScore(), medications: [AMOX] }
  if (drugChart !== undefined) d.drug_chart = drugChart
  return d
}

function chartText(): string {
  // Visible words and the words a button is named by.
  const titles = Array.from(document.querySelectorAll('[title]')).map(e => e.getAttribute('title')).join(' ')
  return `${document.body.textContent || ''} ${titles}`
}

beforeEach(() => {
  vi.clearAllMocks()
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
  h.board = []
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => h.board, headers: { get: () => String(h.board.length) },
  })))
  vi.stubGlobal('print', vi.fn())
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.unstubAllGlobals()
})

describe('the chart on an Orb Ward Record computer', () => {
  beforeEach(() => {
    act(() => { setClinicalSwitches({ scoring_enabled: false, models_enabled: false, ward_record: true }) })
  })

  it('draws no Medications button, no Print medications and no medicine word', async () => {
    h.detail = { ...chartWithNoScore(), drug_chart: false }
    await openChart()
    expect(document.getElementById('btn-meds-reports')).toBeNull()
    expect(screen.queryByRole('button', { name: /print medications/i })).toBeNull()
    expect(chartText()).not.toMatch(MEDICINE_WORDS)
  })

  it('draws nothing of a drug chart an older server still sends', async () => {
    h.detail = withDrugs(undefined)
    await openChart()
    expect(document.getElementById('btn-meds-reports')).toBeNull()
    expect(chartText()).not.toMatch(MEDICINE_WORDS)
  })

  it('Print record has no medicines section, not even an empty one', async () => {
    h.detail = { ...chartWithNoScore(), drug_chart: false }
    await openChart()
    await act(async () => { screen.getByRole('button', { name: /print record/i }).click() })
    const record = screen.getByTestId('inpatient-record-print')
    expect(record.textContent).toMatch(/INPATIENT RECORD/)
    expect(record.textContent).not.toMatch(MEDICINE_WORDS)
  })
})

describe('before the health probe answers', () => {
  it('the chart answer alone keeps the drug lists off', async () => {
    // wardRecord is still its default false: the probe has not answered.
    h.detail = { ...withDrugs(false), medications: [] }
    await openChart()
    expect(document.getElementById('btn-meds-reports')).toBeNull()
    expect(screen.queryByRole('button', { name: /print medications/i })).toBeNull()
    expect(chartText()).not.toMatch(MEDICINE_WORDS)
  })
})

describe('with the profile off', () => {
  it('the chart keeps its Medications list, its form and Print medications', async () => {
    h.detail = withDrugs(true)
    await openChart()
    expect(screen.getByRole('button', { name: /print medications/i })).toBeTruthy()
    await act(async () => { document.getElementById('btn-meds-reports')!.click() })
    expect(await screen.findByText('Amoxicillin')).toBeTruthy()
    await act(async () => { screen.getByRole('button', { name: /print record/i }).click() })
    expect(screen.getByTestId('inpatient-record-print').textContent).toMatch(/ACTIVE MEDICINES/)
  })
})
