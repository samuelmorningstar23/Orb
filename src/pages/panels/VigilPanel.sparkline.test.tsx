/**
 * THE COMMAND-BAR TREND SAYS WHAT SPAN IT ACTUALLY COVERS.
 *
 * Each vital cell in the command bar opens a sparkline. It was headed
 * "HR TREND (6H)" with an x-axis running "-6h" to "now", and the aria-label
 * promised an "open 6-hour trend". The data under it was never six hours: it
 * was the last 40 entries of the chart's vitals array, whatever time those 40
 * entries happened to cover.
 *
 *   Simulator lane: the live socket appends a point every 5 s, so 40 points is
 *   about 200 SECONDS, drawn under a label that says six hours.
 *
 *   Manual ward: a nurse charts every 4 hours, so 40 points is about a WEEK,
 *   and a tachycardia from days ago sets the scale of "the last six hours".
 *
 * WHAT THESE TESTS PIN: the line covers at most the 6 hours that end at the
 * newest observation, positioned by time, and the header and axis state the
 * span and the clock times of the first and last plotted observations, never
 * a fixed "6H" or "now". Both cases open a real chart and read the tooltip
 * that is on the screen.
 */
import { render, screen, act, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openPatient } from '../../lib/orbBus'

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

/* A fixed local noon, so every observation below falls on "today" whatever
   the timezone the suite runs in. */
const NOW = new Date(2026, 9, 3, 12, 0, 0).getTime()
const MIN = 60_000
const HOUR = 60 * MIN

type Vital = {
  timestamp: string; heart_rate: number; systolic_bp: number; diastolic_bp: number
  respiratory_rate: number; temperature: number; spo2: number
}

function vital(atMs: number, heartRate: number): Vital {
  return {
    timestamp: new Date(atMs).toISOString(), heart_rate: heartRate, systolic_bp: 118,
    diastolic_bp: 74, respiratory_rate: 16, temperature: 36.8, spo2: 97,
  }
}

/* Newest first, the order the API returns and the live socket prepends in. */
let VITALS: Vital[] = []

const detail = () => ({
  patient: {
    patient_id: 'P021', name: 'Anil Rao', bed: '21', mrn: 'MRN-P021',
    diagnosis: 'Community-acquired pneumonia', admission_date: '2026-09-29',
    age: 61, sex: 'M', comorbidities: [],
  },
  vitals: VITALS,
  medications: [], reports: [], labs: [], alerts: [], lens_analyses: [], notes: [],
  risk: { level: 'low', score: 0.1, raw_news2: 0, drivers: [], breakdown: [], single_param_3: false },
  trend: { trend: 'stable', reason: null },
})

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({ allergies: [] })),
  getPatientDetail: vi.fn(async () => detail()),
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: (r: { status?: string; allergy_check?: { override?: { reason?: string } | null } | null } | null) =>
    Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
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

beforeEach(() => {
  vi.clearAllMocks()
  // Only Date is faked: the chart's loaders still resolve on real timers.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

afterEach(() => {
  vi.useRealTimers()
})

/** The clock time the app prints, computed here independently of the code
 *  under test. */
const clock = (ms: number) => new Date(ms).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })

async function openHeartRateCell(): Promise<HTMLElement> {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient('P021', 'Anil Rao') })
  await screen.findByText('Anil Rao')
  return waitFor(() => screen.getByLabelText(/^Heart rate/))
}

function tooltipOf(cell: HTMLElement) {
  const tip = cell.querySelector('.command-bar__vital-tooltip') as HTMLElement | null
  if (!tip) throw new Error('no trend tooltip rendered in the heart-rate cell')
  const header = tip.querySelector('.vital-trend-header')!.textContent
  const axis = Array.from(tip.querySelector('[data-trend-axis="x"]')?.children ?? []).map(el => el.textContent)
  const scale = Array.from(tip.querySelector('[data-trend-axis="y"]')?.children ?? []).map(el => el.textContent)
  const polyline = tip.querySelector('polyline')!.getAttribute('points')!.trim().split(/\s+/)
  return { header, axis, scale, polyline }
}

describe('the simulator lane: a point every 5 seconds', () => {
  // 60 points, 5 s apart, newest one minute ago: under five minutes of data.
  const newest = NOW - MIN
  const oldest = newest - 59 * 5_000

  beforeEach(() => {
    VITALS = Array.from({ length: 60 }, (_, i) => vital(newest - i * 5_000, 80 + (i % 7)))
  })

  it('heads the trend with the minutes it covers, not six hours', async () => {
    const { header } = tooltipOf(await openHeartRateCell())
    expect(header).not.toMatch(/6\s*H/i)
    expect(header).toBe('HR TREND (5 MIN)')
  })

  it('labels the axis with the first and last observation times, not "-6h" and "now"', async () => {
    const { axis } = tooltipOf(await openHeartRateCell())
    expect(axis).not.toContain('-6h')
    expect(axis).not.toContain('now')
    expect(axis).toEqual([clock(oldest), clock(newest)])
  })

  it('plots every observation in the span, not only the newest 40', async () => {
    const { polyline } = tooltipOf(await openHeartRateCell())
    expect(polyline).toHaveLength(60)
  })

  it('names the real span in the cell aria-label', async () => {
    const cell = await openHeartRateCell()
    const label = cell.getAttribute('aria-label')!
    expect(label).not.toMatch(/6-hour/)
    // Only the text after the colon belongs to the trend; the command bar owns
    // the value (and any NEWS2 points clause) before it.
    expect(label).toMatch(/^Heart rate 80 bpm\b/)
    expect(label.endsWith(`: open trend, 5 min of observations, ${clock(oldest)} to ${clock(newest)}`)).toBe(true)
  })
})

describe('the manual ward: a set every 4 hours', () => {
  // Five days of 4-hourly sets. The tachycardia (148) is four days old; the
  // last six hours hold two sets, 84 and 90.
  const newest = NOW - 30 * MIN

  beforeEach(() => {
    VITALS = Array.from({ length: 30 }, (_, i) => vital(newest - i * 4 * HOUR, i === 0 ? 84 : i === 1 ? 90 : i === 24 ? 148 : 100))
  })

  it('draws only the six hours that end at the newest set', async () => {
    const { polyline, scale } = tooltipOf(await openHeartRateCell())
    expect(polyline).toHaveLength(2)
    // A four-day-old tachycardia does not set the scale of today's trend.
    expect(scale).toEqual(['90', '84'])
  })

  it('heads the trend with the hours the two sets span', async () => {
    const { header, axis } = tooltipOf(await openHeartRateCell())
    expect(header).toBe('HR TREND (4H)')
    expect(axis).toEqual([clock(newest - 4 * HOUR), clock(newest)])
  })
})

describe('a single observation is not a trend', () => {
  beforeEach(() => {
    VITALS = [vital(NOW - 10 * MIN, 88)]
  })

  it('renders no tooltip and does not offer one in the aria-label', async () => {
    const cell = await openHeartRateCell()
    expect(cell.querySelector('.command-bar__vital-tooltip')).toBeNull()
    const label = cell.getAttribute('aria-label')!
    expect(label).toMatch(/^Heart rate 88 bpm\b/)
    expect(label).not.toMatch(/open|trend/i)
  })
})
