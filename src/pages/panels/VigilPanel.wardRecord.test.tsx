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

/* Review, 7 October 2026. */
describe('what else the chart leaves out with the switches off', () => {
  it('draws no worsening or improving trend line with scoring off', async () => {
    h.detail = { ...chartWithNoScore(), trend: { trend: 'worsening' } }
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    await openChart()
    expect(screen.queryByText(/Trend \(last 6h/)).toBeNull()
    expect(document.querySelector('.detail__trend')).toBeNull()
  })

  it('keeps the trend line with scoring on', async () => {
    h.detail = { ...chartWithNoScore(), trend: { trend: 'worsening' } }
    await openChart()
    expect(document.querySelector('.detail__trend')).toBeTruthy()
  })

  it('under the profile shows no Sepsis Six card and asks no model to forecast or warm Sage', async () => {
    act(() => {
      setClinicalSwitches({ scoring_enabled: false, models_enabled: false, ward_record: true })
    })
    await openChart()
    expect(screen.queryByTestId('care-bundle')).toBeNull()
    expect(vi.mocked(getPatientForecast)).not.toHaveBeenCalled()
    expect(vi.mocked(prefetchSageContext)).not.toHaveBeenCalled()
  })

  it('keeps the Sepsis Six card and Sage warm up without requesting an unused forecast', async () => {
    await openChart()
    expect(screen.getByTestId('care-bundle')).toBeTruthy()
    expect(vi.mocked(getPatientForecast)).not.toHaveBeenCalled()
    expect(vi.mocked(prefetchSageContext)).toHaveBeenCalled()
  })
})

describe('the board sort with scoring switched off', () => {
  it('names the order it really uses: ward and bed, not stability', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    render(
      <ActivePatientProvider>
        <ActiveViewProvider>
          <VigilPanel role="doctor" />
        </ActiveViewProvider>
      </ActivePatientProvider>,
    )
    await waitFor(() => expect(screen.getByText('Admitted · My Dept')).toBeTruthy())
    expect(screen.getByRole('button', { name: /Ward and bed/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Stability/ })).toBeNull()
  })
})

describe('a discharge with the models switched off (review, 9 October 2026)', () => {
  it('says drafting is switched off on this computer, not that the assistant was unavailable', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false, models_enabled: false, ward_record: true }) })
    vi.mocked(dischargePatient).mockResolvedValue({
      status: 'success', summary_id: 51, review_status: 'draft', drafted_by: 'unavailable',
      summary: 'DISCHARGE SUMMARY\nRecorded data only.', discharged_at: T, timeline: [],
      summary_url: null, qr_code_url: null, secure_link_issued: false, secure_link_reason: null,
    } as never)
    await openChart()
    await act(async () => { screen.getByRole('button', { name: /^Discharge$/ }).click() })
    await act(async () => { screen.getByRole('button', { name: /Confirm discharge/ }).click() })
    const draft = await screen.findByTestId('discharge-draft')
    expect(draft).toHaveTextContent('Recorded data only, not reviewed')
    expect(draft.parentElement).toHaveTextContent(
      'The drafting assistant is switched off on this computer, so this holds the recorded data only.')
    expect(draft.parentElement).not.toHaveTextContent('was unavailable')
  })
})

describe('the medication list under the Ward Record profile (9 October 2026)', () => {
  const MED = { id: 7, drug_name: 'Paracetamol', dosage: '1 g', route: 'Oral', frequency: 'QDS', status: 'active', unverified: false }

  it('draws no medications button, no Print medications, no Administer button and no drug, and asks for no administrations', async () => {
    // The drug chart stays on paper under the profile ("hide the drug lists"),
    // even when an older server still sends a medication row.
    h.detail = { ...chartWithNoScore(), medications: [MED] }
    act(() => { setClinicalSwitches({ scoring_enabled: false, models_enabled: false, ward_record: true }) })
    vi.mocked(getAdministrations).mockClear()
    await openChart()
    expect(document.getElementById('btn-meds-reports')).toBeNull()
    expect(screen.queryByRole('button', { name: /print medications/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Administer$/ })).toBeNull()
    expect(screen.queryByText(/Paracetamol/)).toBeNull()
    expect(getAdministrations).not.toHaveBeenCalled()
  })

  it('keeps the Administer button with only scoring off', async () => {
    h.detail = { ...chartWithNoScore(), medications: [MED] }
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    await openChart()
    await act(async () => { document.getElementById('btn-meds-reports')!.click() })
    expect(await screen.findByRole('button', { name: /^Administer$/ })).toBeTruthy()
  })
})
