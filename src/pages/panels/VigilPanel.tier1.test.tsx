/**
 * THE CHART, AFTER THE TWO DEFECTS THE PILOT DRY RUN FOUND ON IT.
 *
 * Both were seen in a real browser on the running ward, and both are about the
 * same thing: what the screen implies when it has nothing true to say.
 *
 *   D2 — A PERMANENT "GENERATING…". Under the deterministic drivers on EVERY
 *        chart sat "✦ MedGemma AI Assessment — Generating…", which never
 *        resolved (150 s dedicated test, 6+ minutes live). It was also opening a
 *        512-token 27B stream per chart open and taking the alert lane's LLM
 *        slot to do it. Removed outright, endpoint and all.
 *   D4 — "Trend: → Stable" ON EVERY PATIENT, including one whose systolic BP
 *        fell 108 → 65 across six observations. Two causes, both here: the
 *        server's fourth trend value (`insufficient_data`, meaning there is no
 *        earlier window to compare against) fell into the `else` branch that
 *        prints "Stable"; and the Tier 1 payload — which carries the real
 *        movement sentences and a deterministic headline — was never read by
 *        any component.
 *
 * These are panel-level properties on purpose. The component suites next door
 * prove the blocks render; what can only be proved HERE is which of them is on
 * this screen, how many of them there are, and what the chart does NOT do when
 * a chart is opened.
 */
import { render, screen, waitFor, act, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { AdministrationRecord } from '../../api/client'
import { openPatient } from '../../lib/orbBus'

const mocks = vi.hoisted(() => ({
  getLatestAssessmentForPatient: vi.fn(),
  listSageAssessments: vi.fn(),
  transitionAssessment: vi.fn(),
}))

/* The removed panel's endpoint. getPatientAnalysis is gone from api/client too,
   so the only way back to it is a raw request; every request the chart makes
   goes through the stubbed fetch or apiFetch below, and none may name it. */
const ANALYSIS_PATH = /\/vigil\/patients\/[^/]+\/analysis\b/
function analysisRequests(): string[] {
  const urls: string[] = []
  for (const call of (globalThis.fetch as unknown as { mock?: { calls: unknown[][] } }).mock?.calls ?? []) {
    urls.push(String(call[0]))
  }
  return urls.filter(u => ANALYSIS_PATH.test(u))
}

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: mocks.getLatestAssessmentForPatient,
  listSageAssessments: mocks.listSageAssessments,
  transitionAssessment: mocks.transitionAssessment,
}))

/** The dry-run patient: systolic 108 → 65 across six observations, and a
 *  trend the server cannot compute because there is no earlier window. */
const DETAIL = {
  patient: {
    patient_id: 'P007', name: 'Rahul Menon', bed: '7', mrn: 'MRN-P007',
    diagnosis: 'Hospital-acquired pneumonia', admission_date: '2026-08-10',
    age: 71, sex: 'M', comorbidities: [],
  },
  vitals: [{
    timestamp: '2026-08-14T09:32:00Z', heart_rate: 128, systolic_bp: 65,
    diastolic_bp: 44, respiratory_rate: 32, temperature: 37.1, spo2: 88,
  }],
  medications: [], notes: [], reports: [], labs: [], alerts: [], lens_analyses: [],
  risk: {
    level: 'critical', score: 0.92, raw_news2: 12,
    drivers: ['Hypotension (BP 65)', 'Tachycardia (HR 128)'],
    breakdown: [
      { parameter: 'Systolic BP', value: 65, points: 3, note: 'Hypotension (BP 65)' },
      { parameter: 'Heart rate', value: 128, points: 2, note: 'Tachycardia (HR 128)' },
      { parameter: 'Temperature', value: 37.1, points: 0, note: null },
    ],
    single_param_3: true,
  },
  // THE VALUE THAT USED TO PRINT AS "STABLE".
  trend: { trend: 'insufficient_data', reason: 'no earlier vitals to compare against' },
}

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({})),
  getPatientDetail: vi.fn(async () => DETAIL),
  getAdministrations: vi.fn(async () => []),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: AdministrationRecord | null | undefined) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  dischargePatient: vi.fn(),
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
vi.mock('../../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import VigilPanel from './VigilPanel'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

const MOVEMENT_SBP =
  'Systolic BP 65mmHg, down from 108mmHg across the last 6 observations, NEWS2 points 1 → 3'
const HEADLINE =
  'NEWS2 12 (critical), up 9 across the last 6 observations. Driven by Systolic BP 65mmHg (3); '
  + 'Heart rate 128 (2). Most abnormal: Systolic BP at 3 points.'

/** The Tier 1 block the server computes, stores and ships on every alert. */
const TIER1 = {
  available: true,
  source: 'orb_deterministic_engine',
  scoring_model: 'NEWS2',
  spo2_scale: 1,
  news2: 12,
  news2_band: 'critical',
  single_param_3: true,
  complete: true,
  missing_parameters: [],
  coverage_note: null,
  observed_at: '2026-08-14T09:32:00Z',
  observations_used: 6,
  window_from: '2026-08-14T04:02:00Z',
  window_to: '2026-08-14T09:32:00Z',
  parameters: [
    {
      parameter: 'Systolic BP', value: 65, points: 3, note: 'Hypotension (BP 65)',
      display: '65mmHg', unit: 'mmHg', previous_value: 82, previous_points: 2,
      first_value: 108, first_points: 1, value_delta: -43, points_delta: 2,
      points_direction: 'worsening', value_direction: 'down', movement: MOVEMENT_SBP,
    },
  ],
  non_scoring: [],
  most_abnormal: null,
  most_abnormal_tied_with: [],
  trend: 'worsening',
  news2_delta: 9,
  computed_at: '2026-08-14T09:32:01Z',
  headline: HEADLINE,
}

function assessment(over: Record<string, unknown> = {}) {
  return {
    id: 'asm-1', patient_id: 'P007', channel_id: 'ch-bed7', message_id: 4021,
    lane: 'ward', state: 'ready', version: 1,
    unavailable_reason: null, unavailable_message: null, fallback_text: null,
    tier1: TIER1, has_tier1: true, has_advice: true,
    news2: 12, news2_level: 'critical', news2_drivers: [],
    whats_happening: 'Worsening septic shock.',
    considerations: [{
      key: 'c0', step: 'Escalate now', why: null, proposed_action_type: null,
      proposed_target: null, orb_can_act: false, action_id: null,
      action_type: null, advice_only_reason: null,
    }],
    evidence: [], citation_basis: 'none', evidence_note: null,
    watch_items: [], confidence: 'low', model: 'medgemma:27b',
    generated_ms: 46300, ttft_ms: 7400,
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    disclaimer: 'Draft decision support. Nothing here has been ordered, charted or administered. '
      + "Every action requires a clinician's explicit approval.",
    ...over,
  }
}

function serve(row: Record<string, unknown> | null) {
  const rows = row ? [row] : []
  mocks.getLatestAssessmentForPatient.mockResolvedValue({
    channelId: 'ch-bed7', assessment: row, rows,
  })
  mocks.listSageAssessments.mockResolvedValue(rows)
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
  await waitFor(() => expect(mocks.getLatestAssessmentForPatient).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.listSageAssessments.mockResolvedValue([])
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

/* ══════════════════════════════════════════════════════════════════════════
   D2 — NOTHING ON THIS CHART IS PERMANENTLY "GENERATING"
   ══════════════════════════════════════════════════════════════════════════ */
describe('the chart opens without starting a generation', () => {
  it('never calls the analysis stream — no 27B run on a chart open', async () => {
    serve(null)
    await openChart()
    // The endpoint is gone from the server; this asserts the CLIENT no longer
    // reaches for it, which is what took the alert lane's LLM slot.
    expect(analysisRequests()).toEqual([])
    const { apiFetch } = await import('../../api/client')
    const viaApi = (apiFetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
      .map(c => String(c[0])).filter(u => ANALYSIS_PATH.test(u))
    expect(viaApi).toEqual([])
  })

  it('shows no "Generating…" shimmer and no MedGemma assessment panel', async () => {
    serve(null)
    await openChart()
    expect(screen.queryByText(/MedGemma AI Assessment/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Generating…/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/AI analysis unavailable/i)).not.toBeInTheDocument()
    // And no skeleton left behind pretending something is on its way.
    expect(document.querySelector('.ai-narrative__expand')).toBeNull()
  })

  it('still shows no generation panel when a draft exists beside it', async () => {
    serve(assessment())
    await openChart()
    await screen.findByText('AI DRAFT')
    expect(analysisRequests()).toEqual([])
    expect(screen.queryByText(/MedGemma AI Assessment/i)).not.toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   D4 — A FALLING PATIENT NEVER READS AS STABLE
   ══════════════════════════════════════════════════════════════════════════ */
describe('the 6-hour trend line', () => {
  it('says there is no baseline instead of printing "Stable"', async () => {
    serve(null)
    await openChart()

    const line = document.querySelector('.detail__trend') as HTMLElement
    expect(line).not.toBeNull()
    expect(line.textContent).toMatch(/not enough earlier observations to compare/i)
    // THE DEFECT: `insufficient_data` used to fall through to "→ Stable" on a
    // patient whose pressure had dropped 43 mmHg.
    expect(line.textContent).not.toMatch(/stable/i)
    // It is not painted as the "steady" state either.
    expect(line.className).not.toMatch(/detail__trend--stable/)
  })

  it('names what it is a trend OF, so it cannot be read as the last observation', async () => {
    serve(null)
    await openChart()
    expect(screen.getByText(/Trend \(last 6h vs the 6h before\)/)).toBeInTheDocument()
  })
})

describe("Tier 1 on the chart", () => {
  it("renders the server's movement sentence and headline — the payload nothing read", async () => {
    serve(assessment())
    await openChart()

    expect(await screen.findByText(MOVEMENT_SBP)).toBeInTheDocument()
    expect(screen.getByText(HEADLINE)).toBeInTheDocument()
    expect(screen.getByText('↘ Worsening')).toBeInTheDocument()
  })

  it('survives a failed generation — the drivers are not the model\'s to lose', async () => {
    serve(assessment({
      state: 'unavailable',
      unavailable_reason: 'queue_timeout',
      unavailable_message: 'Sage was still waiting for the model. No assessment was written.',
      whats_happening: null,
      considerations: [],
      has_advice: false,
    }))
    await openChart()

    // The advice failed; Tier 1 was computed before generation started and is
    // not retractable by it.
    expect(await screen.findByText(
      'Sage was still waiting for the model. No assessment was written.',
    )).toBeInTheDocument()
    expect(screen.getByText(MOVEMENT_SBP)).toBeInTheDocument()
    expect(screen.getByText(HEADLINE)).toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   ONE BREAKDOWN ON THE SCREEN, NEVER TWO
   ══════════════════════════════════════════════════════════════════════════ */
describe('how many components render the driver breakdown', () => {
  it('is exactly one when the alert has a Tier 1 block — the richer one wins', async () => {
    serve(assessment())
    await openChart()
    await screen.findByText(MOVEMENT_SBP)

    const blocks = document.querySelectorAll('.news2-drivers')
    expect(blocks).toHaveLength(1)
    // And it is the Tier 1 one: the live chart copy stood down rather than
    // printing a second, differently-timed answer to the same question.
    expect(blocks[0].getAttribute('data-source')).toBe('tier1')
  })

  it('is exactly one when there is no draft at all — every chart keeps a breakdown', async () => {
    serve(null)
    await openChart()

    const blocks = document.querySelectorAll('.news2-drivers')
    expect(blocks).toHaveLength(1)
    expect(blocks[0].getAttribute('data-source')).toBe('chart')
    // Scoped to the block: the same engine string also appears in the gauge's
    // top-three driver list beside it, which is a different (and much shorter)
    // statement — the breakdown is the one that has to be complete.
    expect(within(blocks[0] as HTMLElement).getByText('Hypotension (BP 65)')).toBeInTheDocument()
    expect(within(blocks[0] as HTMLElement).getByText('Tachycardia (HR 128)')).toBeInTheDocument()
  })

  it('gives the chart its breakdown back when the draft is read but has no Tier 1', async () => {
    // An older row written before Tier 1 existed. "No tier1" must degrade to the
    // live breakdown, never to no breakdown at all.
    serve(assessment({ tier1: undefined, has_tier1: false }))
    await openChart()
    await screen.findByText('AI DRAFT')

    const blocks = document.querySelectorAll('.news2-drivers')
    expect(blocks).toHaveLength(1)
    expect(blocks[0].getAttribute('data-source')).toBe('chart')
  })
})
