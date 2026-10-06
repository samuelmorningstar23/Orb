/**
 * The Vigil chart and board with scoring switched off (the Orb Ward Record
 * profile, 6 October 2026).
 *
 * GET /api/vigil/patients/{id} then sends `risk: null` and says why in
 * `risk_off_reason`. Every badge, band colour, gauge, breakdown and score line
 * on the chart header, the vitals command bar and the board must draw nothing
 * for a null score: not a dash, not a 0, not "stable". The sentence saying why
 * is shown once, where the score panel was. A live set pushed with no score
 * still moves the bar, and under the profile the chart offers no handover.
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
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
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

async function emit(event: string, payload: unknown) {
  await act(async () => { for (const fn of [...(h.handlers[event] || [])]) fn(payload) })
}

const commandBar = () => document.querySelector('.command-bar') as HTMLElement

beforeEach(() => {
  vi.clearAllMocks()
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
  h.detail = chartWithNoScore()
  h.board = []
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => h.board, headers: { get: () => String(h.board.length) },
  })))
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.unstubAllGlobals()
})

describe('the Vigil chart with scoring switched off', () => {
  beforeEach(() => { act(() => { setClinicalSwitches({ scoring_enabled: false }) }) })

  it('the header badge is the bed alone: no band, no number, no dash', async () => {
    await openChart()
    const badge = document.querySelector('.command-bar__bed-badge') as HTMLElement
    expect(badge.textContent).toBe('BED 7')
    expect(badge.className).toMatch(/command-bar__bed-badge--none/)
    expect(badge.className).not.toMatch(/--(critical|elevated|stable)/)
    expect(document.querySelector('.command-bar__risk-pct')).toBeNull()
  })

  it('the vitals command bar shows the readings with no points and no colour', async () => {
    await openChart()
    const bar = commandBar()
    expect(bar.textContent).not.toMatch(/NEWS2/)
    expect(bar.querySelectorAll('.command-bar__vital-points').length).toBe(0)
    expect(bar.querySelectorAll('[class*="command-bar__vital-scored--"]').length).toBe(0)
    expect(bar.querySelectorAll('[data-news2-points]').length).toBe(0)
    expect(bar.textContent).toContain('135')
  })

  it('says once why there is no score, and draws no gauge, drivers or breakdown', async () => {
    await openChart()
    expect(screen.getByTestId('chart-score-off').textContent).toBe(REASON)
    expect(screen.queryByText('Why this score')).toBeNull()
    expect(document.querySelector('.risk-gauge')).toBeNull()
    expect(document.querySelector('.detail__driver')).toBeNull()
  })

  it('a set pushed live with no score still moves the bar', async () => {
    await openChart()
    await emit('vitals:update', { ...VITAL, timestamp: '2026-10-06T09:00:00Z', heart_rate: 99, risk: null })
    await waitFor(() => expect(commandBar().textContent).toContain('99'))
    expect(commandBar().textContent).not.toMatch(/NEWS2/)
  })

  it('still offers the handover while only scoring is off', async () => {
    await openChart()
    expect(screen.getByRole('button', { name: /Handoff/ })).toBeTruthy()
  })
})

describe('the Vigil chart under the Ward Record profile', () => {
  beforeEach(() => {
    act(() => {
      setClinicalSwitches({ scoring_enabled: false, models_enabled: false, ward_record: true })
    })
  })

  it('offers no handover and asks no model for the expected course', async () => {
    await openChart()
    expect(screen.queryByRole('button', { name: /Handoff/ })).toBeNull()
    expect(screen.getByRole('button', { name: /Downtime/ })).toBeTruthy()
    expect(screen.queryByTestId('course')).toBeNull()
    expect(h.getTimelineProjection).not.toHaveBeenCalled()
  })
})

describe('the patient list with scoring switched off', () => {
  function board() {
    render(
      <ActivePatientProvider>
        <ActiveViewProvider>
          <VigilPanel role="doctor" />
        </ActiveViewProvider>
      </ActivePatientProvider>,
    )
  }

  it('draws no Elevated or Critical tile, not a 0, and no brief or insights over the list', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    board()
    await waitFor(() => expect(screen.getByText('Admitted · My Dept')).toBeTruthy())
    expect(screen.queryByText('Elevated')).toBeNull()
    expect(screen.queryByText('Critical')).toBeNull()
    expect(screen.queryByTestId('ward-brief')).toBeNull()
  })

  it('keeps the tiles, the brief and the feed with the switches on', async () => {
    board()
    await waitFor(() => expect(screen.getByText('Admitted · My Dept')).toBeTruthy())
    expect(screen.getByText('Elevated')).toBeTruthy()
    expect(screen.getByText('Critical')).toBeTruthy()
    expect(screen.getByTestId('ward-brief')).toBeTruthy()
    expect(screen.getByTestId('insights-feed')).toBeTruthy()
  })

  it('asks for no Sage insights with the models switched off', async () => {
    act(() => { setClinicalSwitches({ models_enabled: false }) })
    board()
    await waitFor(() => expect(screen.getByText('Admitted · My Dept')).toBeTruthy())
    expect(screen.queryByTestId('insights-feed')).toBeNull()
  })
})
