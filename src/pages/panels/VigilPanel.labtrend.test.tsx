/**
 * THE LAB TREND ARROW IS NOT ALLOWED TO INVERT A CLINICAL MEANING.
 *
 * Seen in a real browser on the running ward, 2026-08-14, on two charts:
 *
 *   Priya Nair, admitted with DENGUE. PLATELETS 93 (ref 150–400), FALLING.
 *   Rendered ↓ in --status-ok — GREEN. Falling platelets in dengue is the one
 *   thing that ward watches for, and the chart painted it as reassurance.
 *
 *   Lakshmi Devi. HAEMOGLOBIN 9.6 (ref 12–16), RISING back toward normal.
 *   Rendered ↑ in --status-warn — an AMBER WARNING for a patient improving.
 *
 * One expression caused both: the arrow was coloured by DIRECTION alone
 * (up = warn, down = ok), which is only ever right for analytes where high is
 * the danger and is exactly inverted for the ones where low is.
 *
 * WHAT THESE TESTS PIN is the invariant, not the implementation: colour comes
 * from movement RELATIVE TO THE REFERENCE RANGE, so no analyte anywhere can be
 * coloured against its own clinical meaning. The table below runs the real
 * analytes from both directions — platelets/haemoglobin/sodium (low is the
 * danger) and creatinine/lactate/potassium (high is) — through one rule, and
 * the last group covers what the screen must do when it has nothing to judge
 * against.
 *
 * The DOM case at the bottom is the actual defect: it opens Priya Nair's chart
 * with her real seeded lab document and reads the colour off the arrow that is
 * on the screen, because a pure function can be right while the chart still
 * paints the old value.
 */
import { render, screen, act, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { openPatient } from '../../lib/orbBus'

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

/* Priya Nair's chart, as the ward saw it: two lab documents twelve hours apart.
   PLATELETS fall 138 → 93 with the range 150–400 on the line, exactly the shape
   VigilPanel's lab-document parser reads. HAEMOGLOBIN rises 8.9 → 9.6 toward
   12–16, and CREATININE rises 1.6 → 2.1 above 0.7–1.3, so one chart carries all
   three of the analyte cases at once. */
const LAB_EARLIER = [
  '  PLATELETS: 138 ×10³/µL  LOW  (ref: 150-400)',
  '  HAEMOGLOBIN: 8.9 g/dL  LOW  (ref: 12-16)',
  '  CREATININE: 1.6 mg/dL  HIGH  (ref: 0.7-1.3)',
  '  SODIUM: 139 mEq/L  (ref: 136-145)',
].join('\n')

const LAB_LATER = [
  '  PLATELETS: 93 ×10³/µL  LOW  (ref: 150-400)',
  '  HAEMOGLOBIN: 9.6 g/dL  LOW  (ref: 12-16)',
  '  CREATININE: 2.1 mg/dL  HIGH  (ref: 0.7-1.3)',
  '  SODIUM: 141 mEq/L  (ref: 136-145)',
].join('\n')

const DETAIL = {
  patient: {
    patient_id: 'P011', name: 'Priya Nair', bed: '11', mrn: 'MRN-P011',
    diagnosis: 'Dengue fever', admission_date: '2026-08-11',
    age: 29, sex: 'F', comorbidities: [],
  },
  vitals: [{
    timestamp: '2026-08-14T09:00:00Z', heart_rate: 96, systolic_bp: 104,
    diastolic_bp: 68, respiratory_rate: 18, temperature: 38.4, spo2: 97,
  }],
  medications: [], reports: [], labs: [], alerts: [], lens_analyses: [],
  notes: [
    { id: 1, doc_type: 'lab_result', content: LAB_EARLIER, created_at: '2026-08-13T21:00:00Z' },
    { id: 2, doc_type: 'lab_result', content: LAB_LATER, created_at: '2026-08-14T09:00:00Z' },
  ],
  risk: { level: 'elevated', score: 0.4, raw_news2: 3, drivers: [], breakdown: [], single_param_3: false },
  trend: { trend: 'stable', reason: null },
}

/* A test that needs a different chart sets this; beforeEach puts it back. */
let detailOverride: unknown = null

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({ allergies: [] })),
  getPatientDetail: vi.fn(async () => detailOverride ?? DETAIL),
  getAdministrations: vi.fn(async () => []),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: any) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
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
import { labTrendTone, LAB_TREND_TONE_COLOR, labTrendLabel } from '../../lib/labTrends'
import { ActivePatientProvider } from '../../contexts/ActivePatientContext'
import { ActiveViewProvider } from '../../contexts/ActiveViewContext'

/* ══════════════════════════════════════════════════════════════════════════
   THE RULE, OVER REAL ANALYTES FROM BOTH DIRECTIONS
   ══════════════════════════════════════════════════════════════════════════ */
type Case = {
  analyte: string
  why: string
  points: number[]
  refLow: number | null
  refHigh: number | null
  expect: 'reassuring' | 'concerning' | 'neutral'
}

const CASES: Case[] = [
  // ── Low is the danger. Direction alone gets every one of these backwards. ──
  {
    analyte: 'Platelets (dengue)',
    why: 'THE DEFECT: 138 → 93 with the range at 150–400 is falling AWAY from the range',
    points: [138, 93], refLow: 150, refHigh: 400, expect: 'concerning',
  },
  {
    analyte: 'Platelets, recovering',
    why: '93 → 118 is still low but moving toward the range',
    points: [93, 118], refLow: 150, refHigh: 400, expect: 'reassuring',
  },
  {
    analyte: 'Haemoglobin',
    why: 'THE DEFECT, mirrored: 8.9 → 9.6 toward 12–16 is a patient improving',
    points: [8.9, 9.6], refLow: 12, refHigh: 16, expect: 'reassuring',
  },
  {
    analyte: 'Haemoglobin, bleeding',
    why: '9.6 → 8.1 moves further below the range',
    points: [9.6, 8.1], refLow: 12, refHigh: 16, expect: 'concerning',
  },
  {
    analyte: 'Sodium',
    why: '131 → 128 is a hyponatraemia deepening, and it is a FALL',
    points: [131, 128], refLow: 136, refHigh: 145, expect: 'concerning',
  },
  // ── High is the danger. Same rule, no analyte table anywhere. ──
  {
    analyte: 'Creatinine',
    why: '1.6 → 2.1 climbs further above 0.7–1.3',
    points: [1.6, 2.1], refLow: 0.7, refHigh: 1.3, expect: 'concerning',
  },
  {
    analyte: 'Creatinine, resolving',
    why: '2.1 → 1.5 falls back toward the range',
    points: [2.1, 1.5], refLow: 0.7, refHigh: 1.3, expect: 'reassuring',
  },
  {
    analyte: 'Lactate',
    why: '2.4 → 4.8 above 0.5–2.0 is a shocked patient',
    points: [2.4, 4.8], refLow: 0.5, refHigh: 2.0, expect: 'concerning',
  },
  {
    analyte: 'Potassium',
    why: '5.4 → 6.1 above 3.5–5.0',
    points: [5.4, 6.1], refLow: 3.5, refHigh: 5.0, expect: 'concerning',
  },
  // ── Inside the range, and nothing to judge against. ──
  {
    analyte: 'Sodium, in range',
    why: 'a value inside the range moving within it is neither good nor bad news',
    points: [139, 141], refLow: 136, refHigh: 145, expect: 'neutral',
  },
  {
    analyte: 'Potassium leaving the range',
    why: '4.9 (in range) → 5.4 (out) starts a distance where there was none',
    points: [4.9, 5.4], refLow: 3.5, refHigh: 5.0, expect: 'concerning',
  },
  {
    analyte: 'Platelets returning to range',
    why: '142 (out) → 155 (in) ends the distance entirely',
    points: [142, 155], refLow: 150, refHigh: 400, expect: 'reassuring',
  },
  {
    analyte: 'Troponin with no reference range',
    why: 'nothing to be closer to or further from — the arrow must say nothing',
    points: [0.9, 2.4], refLow: null, refHigh: null, expect: 'neutral',
  },
  {
    analyte: 'A single result',
    why: 'one point is not a trend',
    points: [93], refLow: 150, refHigh: 400, expect: 'neutral',
  },
  {
    analyte: 'Unchanged out of range',
    why: '93 → 93 is not improvement and not deterioration',
    points: [93, 93], refLow: 150, refHigh: 400, expect: 'neutral',
  },
]

describe('lab trend arrow colour is judged against the reference range', () => {
  it.each(CASES)('$analyte — $why', ({ points, refLow, refHigh, expect: want }) => {
    expect(labTrendTone({ points, refLow, refHigh })).toBe(want)
  })

  /* The two ward observations, stated as the thing that must never be true
     again rather than as an expected value — a future rewrite that renames the
     tones still has to satisfy these. */
  it('falling platelets in dengue are NEVER coloured as reassurance', () => {
    const tone = labTrendTone({ points: [138, 93], refLow: 150, refHigh: 400 })
    expect(tone).not.toBe('reassuring')
    expect(LAB_TREND_TONE_COLOR[tone]).not.toBe(LAB_TREND_TONE_COLOR.reassuring)
    expect(LAB_TREND_TONE_COLOR[tone]).not.toContain('--status-ok')
  })

  it('haemoglobin rising toward its range is NEVER coloured as a warning', () => {
    const tone = labTrendTone({ points: [8.9, 9.6], refLow: 12, refHigh: 16 })
    expect(tone).not.toBe('concerning')
    expect(LAB_TREND_TONE_COLOR[tone]).not.toContain('--status-warn')
  })

  it('a lab with no reference range renders the neutral token, not a status one', () => {
    expect(LAB_TREND_TONE_COLOR[labTrendTone({ points: [0.9, 2.4] })]).toBe('var(--text-muted)')
  })

  /* Colour is never the only carrier of a clinical judgement. */
  it('says in words what the arrow means, including when it means nothing', () => {
    expect(labTrendLabel('concerning', 'down', { refLow: 150, refHigh: 400 }))
      .toBe('falling away from the reference range')
    expect(labTrendLabel('reassuring', 'up', { refLow: 12, refHigh: 16 }))
      .toBe('rising toward the reference range')
    expect(labTrendLabel('neutral', 'up', {})).toBe('rising, no reference range on file')
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   AND ON THE CHART ITSELF
   ══════════════════════════════════════════════════════════════════════════ */
beforeEach(() => {
  vi.clearAllMocks()
  detailOverride = null
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

async function openChart() {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient('P011', 'Priya Nair') })
  await screen.findByText('Priya Nair')
}

/** The rendered arrow for one analyte tile, found through the tile's own
 *  accessible name so the test cannot drift onto a different lab. */
async function arrowFor(analyte: string): Promise<HTMLElement> {
  const tile = await screen.findByRole('button', { name: new RegExp(`Open trend chart for ${analyte}`, 'i') })
  const arrow = tile.querySelector('.lab-trend-block__arrow')
  if (!arrow) throw new Error(`no trend arrow rendered for ${analyte}`)
  return arrow as HTMLElement
}

describe("Priya Nair's chart, as the ward sees it", () => {
  it('paints her falling platelets as a concern, not as reassurance', async () => {
    await openChart()
    const arrow = await waitFor(() => arrowFor('PLATELETS'))
    expect(arrow).toHaveTextContent('↓')
    expect(arrow.dataset.trendTone).toBe('concerning')
    // The literal defect: --status-ok on a falling platelet count in dengue.
    expect(arrow.getAttribute('style')).not.toContain('--status-ok')
    expect(arrow.getAttribute('title')).toBe('falling away from the reference range')
  })

  it('paints her rising haemoglobin as reassurance, not as a warning', async () => {
    await openChart()
    const arrow = await waitFor(() => arrowFor('HAEMOGLOBIN'))
    expect(arrow).toHaveTextContent('↑')
    expect(arrow.dataset.trendTone).toBe('reassuring')
    expect(arrow.getAttribute('style')).not.toContain('--status-warn')
  })

  it('still warns on a rising creatinine — the same rule, opposite analyte', async () => {
    await openChart()
    const arrow = await waitFor(() => arrowFor('CREATININE'))
    expect(arrow).toHaveTextContent('↑')
    expect(arrow.dataset.trendTone).toBe('concerning')
  })

  it('leaves a sodium moving inside its own range uncoloured', async () => {
    await openChart()
    const arrow = await waitFor(() => arrowFor('SODIUM'))
    expect(arrow.dataset.trendTone).toBe('neutral')
    expect(arrow.getAttribute('style')).toContain('--text-muted')
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   A SIMULATED STRUCTURED RESULT CARRIES THE HONESTY LABEL (lis-5, part a)
   The chart route returns each observation's source. Observation points used
   to drop it, so a result the simulator wrote showed with no "Simulated"
   badge, as if it were this patient's. Lab documents were already labelled.
   ══════════════════════════════════════════════════════════════════════════ */
describe('structured results on the chart', () => {
  const withObservations = (rows: unknown[]) => ({ ...DETAIL, notes: [], lab_observations: rows })
  const potassium = (over: Record<string, unknown>) => ({
    id: 1, observation_name: 'Potassium', value_numeric: 5.9, unit: 'mmol/L',
    reference_range: '3.5-5.1', abnormal_flag: 'H', status: 'final',
    source: 'ingested_hl7', observed_at: '2026-10-03 09:00:00', ...over,
  })

  it('labels a simulated result on its tile', async () => {
    detailOverride = withObservations([potassium({ source: 'simulated' })])
    await openChart()
    const tile = await screen.findByRole('button', { name: /Open trend chart for Potassium/i })
    expect(within(tile).getByRole('note')).toHaveTextContent('Simulated')
  })

  it('leaves an interfaced result unlabelled', async () => {
    detailOverride = withObservations([potassium({ source: 'ingested_hl7' })])
    await openChart()
    const tile = await screen.findByRole('button', { name: /Open trend chart for Potassium/i })
    expect(within(tile).queryByRole('note')).toBeNull()
    expect(tile).toHaveTextContent('5.9')
  })
})
