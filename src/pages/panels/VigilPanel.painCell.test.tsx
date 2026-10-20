/**
 * THE PAIN SCORE ON THE CHART (21 October 2026, founding ward item 1).
 *
 * The command bar shows the sheet's pain score beside the vital signs and
 * trends it like them, and never scores it: no points chip, no colour, no
 * NEWS2 clause in its label. A row without the column reads "not recorded".
 */
import { render, screen, act, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openPatient } from '../../lib/orbBus'

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

const NOW = new Date(2026, 9, 21, 12, 0, 0).getTime()
const HOUR = 3_600_000

type Vital = {
  timestamp: string; heart_rate: number; systolic_bp: number; diastolic_bp: number
  respiratory_rate: number; temperature: number; spo2: number; pain_score?: number | null
}

function vital(atMs: number, painScore: number | null | undefined): Vital {
  const row: Vital = {
    timestamp: new Date(atMs).toISOString(), heart_rate: 80, systolic_bp: 118,
    diastolic_bp: 74, respiratory_rate: 16, temperature: 36.8, spo2: 97,
  }
  if (painScore !== undefined) row.pain_score = painScore
  return row
}

/* Newest first, the order the API returns. */
let VITALS: Vital[] = []

const detail = () => ({
  patient: {
    patient_id: 'P031', name: 'Meena Pillai', bed: '31', mrn: 'MRN-P031',
    diagnosis: 'Post-operative day 2', admission_date: '2026-10-19',
    age: 47, sex: 'F', comorbidities: [],
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
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

afterEach(() => {
  vi.useRealTimers()
})

async function openPainCell(): Promise<HTMLElement> {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient('P031', 'Meena Pillai') })
  await screen.findByText('Meena Pillai')
  return waitFor(() => screen.getByLabelText(/^Pain score/))
}

describe('the pain cell', () => {
  it('shows the newest score out of 10 and trends the sets that carry one', async () => {
    VITALS = [vital(NOW - HOUR, 3), vital(NOW - 3 * HOUR, 6), vital(NOW - 5 * HOUR, 8)]
    const cell = await openPainCell()
    expect(cell.textContent).toContain('3')
    expect(cell.textContent).toContain('/10')
    expect(cell.getAttribute('aria-label')).toMatch(/^Pain score 3 out of 10: open pain trend/)
    // Never scored: no NEWS2 clause, no points chip, no colour token.
    expect(cell.getAttribute('aria-label')).not.toMatch(/NEWS2 point/)
    expect(cell.querySelector('.command-bar__vital-points')).toBeNull()
    expect(cell.querySelector('.command-bar__vital-scored--warn, .command-bar__vital-scored--danger')).toBeNull()
    const tip = cell.querySelector('.command-bar__vital-tooltip') as HTMLElement
    expect(tip.querySelector('.vital-trend-header')!.textContent).toMatch(/^PAIN TREND/)
    const points = tip.querySelector('polyline')!.getAttribute('points')!.trim().split(/\s+/)
    expect(points).toHaveLength(3)
    const scale = Array.from(tip.querySelector('[data-trend-axis="y"]')!.children).map(el => el.textContent)
    expect(scale).toEqual(['8', '3'])
  })

  it('reads "not recorded" on a chart whose rows predate the column, and offers no trend', async () => {
    VITALS = [vital(NOW - HOUR, undefined), vital(NOW - 3 * HOUR, undefined)]
    const cell = await openPainCell()
    expect(cell.getAttribute('aria-label')).toBe('Pain score not recorded out of 10')
    expect(cell.querySelector('.command-bar__vital-tooltip')).toBeNull()
    // The vital signs beside it are untouched.
    expect(screen.getByLabelText(/^Heart rate/).textContent).toContain('80')
  })

  it('a blank box on the newest set reads "not recorded" while older scores still trend', async () => {
    VITALS = [vital(NOW - HOUR, null), vital(NOW - 2 * HOUR, 5), vital(NOW - 4 * HOUR, 7)]
    const cell = await openPainCell()
    expect(cell.getAttribute('aria-label')).toMatch(/^Pain score not recorded out of 10: open pain trend/)
    const points = cell.querySelector('polyline')!.getAttribute('points')!.trim().split(/\s+/)
    expect(points).toHaveLength(2)
  })
})
