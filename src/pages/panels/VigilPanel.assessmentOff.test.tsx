/**
 * The chart's Sage alert assessment is not mounted while its routes cannot
 * answer. relay_routes._assessment_switched_on answers 404 Switched off with
 * scoring or the models switched off, which every Orb Ward Record computer
 * has, and the block asked and then polled that route on every chart opened
 * (seen in a browser under the profile, 9 October 2026).
 */
import { render, screen, act, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openPatient } from '../../lib/orbBus'
import { getLatestAssessmentForPatient, listSageAssessments } from '../../api/sageAssessment'

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


beforeEach(() => {
  vi.clearAllMocks()
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
  h.board = []
  h.detail = chartWithNoScore()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => h.board, headers: { get: () => String(h.board.length) },
  })))
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.unstubAllGlobals()
})

function asked(): number {
  return vi.mocked(getLatestAssessmentForPatient).mock.calls.length
    + vi.mocked(listSageAssessments).mock.calls.length
}

describe('the alert assessment on the chart', () => {
  it('is not asked for on an Orb Ward Record computer, which forces both off', async () => {
    act(() => { setClinicalSwitches({ ward_record: true }) })
    await openChart()
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(asked()).toBe(0)
  })

  it('is not asked for with the models switched off alone', async () => {
    act(() => { setClinicalSwitches({ models_enabled: false }) })
    await openChart()
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(asked()).toBe(0)
  })

  it('is not asked for with scoring switched off alone', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    await openChart()
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(asked()).toBe(0)
  })

  it('is asked for with every switch on', async () => {
    await openChart()
    await waitFor(() => expect(asked()).toBeGreaterThan(0))
  })
})
