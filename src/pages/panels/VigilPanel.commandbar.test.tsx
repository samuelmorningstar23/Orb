/**
 * THE COMMAND BAR COLOURS A NUMBER ONLY WHEN NEWS2 SCORED IT.
 *
 * The five vital cells at the top of the chart were coloured by cutoffs written
 * into the JSX (HR > 100 or < 50, SBP > 160 or < 90, SpO2 < 94, Temp > 38.5,
 * RR > 24). Directly under them the chart prints the NEWS2 breakdown, and the
 * two disagreed on the ward's most ordinary numbers:
 *
 *   SBP 90 scores 3 (the single-parameter escalation) and was left plain.
 *   RR 22 scores 2, RR 8 scores 3, Temp 34.9 scores 3: all plain.
 *   SBP 170 scores 0 and was painted amber.
 *   A Scale 2 patient sitting at 90%, inside their own target, was painted
 *   amber; the same patient at 97% on oxygen scores 3 and was left plain.
 *   An UNMEASURED heart rate was painted amber, because null < 50 is true.
 *
 * A bar that says something different from the breakdown under it teaches the
 * reader to trust neither. Each cell now reads its own row from the risk it is
 * shown with: a visible "+N" chip and a status-text colour when the row scores,
 * nothing when it scores 0 or when there is no row at all.
 *
 * Every assertion below goes through `data-news2-points`, the chip text and the
 * colour classes (old and new), so a regression to the cutoff classes fails.
 */
import { render, screen, act, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { openPatient } from '../../lib/orbBus'
import { NO_VALUE } from '../../lib/vitals'

const h = vi.hoisted(() => {
  const handlers: Record<string, Array<(p: unknown) => void>> = {}
  return {
    detail: null as unknown,
    handlers,
    getLatestAssessmentForPatient: vi.fn(),
    listSageAssessments: vi.fn(),
    /* CAPTURES its handlers. The ward board and the open chart both register
       'vitals:update'; `emit` below calls every one, as the server would. */
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
  getLatestAssessmentForPatient: h.getLatestAssessmentForPatient,
  listSageAssessments: h.listSageAssessments,
  transitionAssessment: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({ allergies: [] })),
  getPatientDetail: vi.fn(async () => h.detail),
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: (r: { status?: string; allergy_check?: { override?: { reason?: string } } } | null) =>
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
  socket: h.socket,
}))

vi.mock('../../components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('../../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import VigilPanel from './VigilPanel'
import News2Drivers from '../../components/vigil/News2Drivers'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

/* ─── Fixtures in the server's shapes ─────────────────────────────────────── */

const T = '2026-10-02T08:00:00Z'
const PATIENT = {
  patient_id: 'P007', name: 'Rahul Menon', bed: '7', mrn: 'MRN-P007',
  diagnosis: 'Community-acquired pneumonia', admission_date: '2026-09-30',
  age: 71, sex: 'M', comorbidities: [],
}

type Row = { parameter: string; value: unknown; points: number; note: string | null; severe: boolean }

/** One breakdown row exactly as vigil_service.calculate_news2 writes it. */
const row = (parameter: string, value: unknown, points: number, note: string | null = null, severe = false): Row =>
  ({ parameter, value, points, note, severe })

/** The risk envelope around hand-written rows. The rows are the fixture; this
 *  only adds up what they say, in the engine's bands. `missing` lists the
 *  parameters nobody measured, which is what makes a set incomplete. */
function risk(rows: Row[], { scale2 = false, missing = [] as string[] } = {}) {
  const raw = rows.reduce((n, r) => n + r.points, 0)
  const any3 = rows.some(r => r.points === 3)
  const level = raw >= 7 ? 'critical' : raw >= 5 || any3 ? 'elevated' : 'stable'
  return {
    score: Math.min(1, raw / 20), level, raw_news2: raw, raw_mews: raw, single_param_3: any3,
    drivers: rows.filter(r => r.points > 0).map(r => r.note),
    complete: missing.length === 0, missing_parameters: missing,
    coverage_note: missing.length ? `INCOMPLETE observation set: ${missing.join(', ')} not measured.` : null,
    breakdown: rows, scoring_model: 'NEWS2', spo2_scale: scale2 ? 2 : 1,
  }
}

type Vital = Record<string, unknown>
const vital = (over: Vital = {}): Vital => ({
  id: 501, patient_id: 'P007', timestamp: T,
  heart_rate: 88, systolic_bp: 124, diastolic_bp: 78, spo2: 96, temperature: 37.0, respiratory_rate: 16,
  consciousness: 'alert', on_oxygen: 0, source: 'manual',
  ...over,
})

function chart(v: Vital | null, r: Record<string, unknown>) {
  return {
    patient: PATIENT, vitals: v ? [v] : [], medications: [], notes: [], reports: [], labs: [],
    alerts: [], lens_analyses: [], risk: r, trend: { trend: 'insufficient_data' },
  }
}

/* ─── Sage's Tier 1 block, for the one case that turns it on ─────────────── */

const MOVEMENT_SBP = 'Systolic BP 65mmHg, down from 108mmHg across the last 6 observations, NEWS2 points 1 → 3'
const TIER1 = {
  available: true, source: 'orb_deterministic_engine', scoring_model: 'NEWS2', spo2_scale: 1,
  news2: 3, news2_band: 'elevated', single_param_3: true, complete: true, missing_parameters: [],
  coverage_note: null, observed_at: T, observations_used: 6,
  window_from: '2026-10-02T02:30:00Z', window_to: T,
  parameters: [{
    parameter: 'Systolic BP', value: 65, points: 3, note: 'Severe hypotension (SBP 65)',
    display: '65mmHg', unit: 'mmHg', previous_value: 82, previous_points: 2,
    first_value: 108, first_points: 1, value_delta: -43, points_delta: 2,
    points_direction: 'worsening', value_direction: 'down', movement: MOVEMENT_SBP,
  }],
  non_scoring: [], most_abnormal: null, most_abnormal_tied_with: [], trend: 'worsening',
  news2_delta: 2, computed_at: T, headline: 'NEWS2 3 (elevated).',
}
const ASSESSMENT = {
  id: 'asm-1', patient_id: 'P007', channel_id: 'ch-bed7', message_id: 4021, lane: 'ward',
  state: 'ready', version: 1, unavailable_reason: null, unavailable_message: null, fallback_text: null,
  tier1: TIER1, has_tier1: true, has_advice: true, news2: 3, news2_level: 'elevated', news2_drivers: [],
  whats_happening: 'Falling pressure.', considerations: [], evidence: [], citation_basis: 'none',
  evidence_note: null, watch_items: [], confidence: 'low', model: 'medgemma:27b',
  generated_ms: 46300, ttft_ms: 7400,
  created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  disclaimer: 'Draft decision support. Nothing here has been ordered, charted or administered.',
}

function serveAssessment(a: Record<string, unknown> | null) {
  const rows = a ? [a] : []
  h.getLatestAssessmentForPatient.mockResolvedValue({ channelId: 'ch-bed7', assessment: a, rows })
  h.listSageAssessments.mockResolvedValue(rows)
}

/* ─── Reading the bar ─────────────────────────────────────────────────────── */

const CELL = {
  hr: 'Heart rate', bp: 'Blood pressure', spo2: 'Oxygen saturation', temp: 'Temperature', rr: 'Respiratory rate',
} as const
type CellKey = keyof typeof CELL

function cell(key: CellKey): HTMLElement {
  const el = document.querySelector(`.command-bar__vital[aria-label^="${CELL[key]}"]`) as HTMLElement | null
  if (!el) throw new Error(`no command-bar cell labelled "${CELL[key]}…"`)
  return el
}

/** Every class that has ever painted a bar number: the old cutoff modifiers on
 *  the cell, and the scored-number modifiers that replaced them. */
const PAINT = /\bcommand-bar__vital--(warn|danger)\b|\bcommand-bar__vital-scored--(warn|danger)\b/
const painted = (el: HTMLElement) =>
  [el, ...Array.from(el.querySelectorAll<HTMLElement>('*'))].filter(e => PAINT.test(e.getAttribute('class') || ''))

/** The status tone the scored number is painted in, or null when unpainted. */
function tone(key: CellKey): 'warn' | 'danger' | null {
  const el = cell(key).querySelector('[class*="command-bar__vital-scored--"]')
  const m = /command-bar__vital-scored--(warn|danger)/.exec(el?.getAttribute('class') || '')
  return (m?.[1] as 'warn' | 'danger' | undefined) ?? null
}

const points = (key: CellKey) => cell(key).getAttribute('data-news2-points')
const chip = (key: CellKey) => cell(key).querySelector('.command-bar__vital-points') as HTMLElement | null
const value = (key: CellKey) => cell(key).querySelector('.command-bar__vital-value')!.textContent
const label = (key: CellKey) => cell(key).getAttribute('aria-label') || ''

/** A scoring cell, all four cues at once. */
function expectScored(key: CellKey, p: 1 | 2 | 3) {
  expect(tone(key)).toBe(p === 3 ? 'danger' : 'warn')
  expect(chip(key)?.textContent).toBe(`+${p}`)
  expect(chip(key)?.className).toMatch(new RegExp(`\\bcommand-bar__vital-points--${p}\\b`))
  expect(points(key)).toBe(String(p))
}

/** A cell that must carry no cue at all. `rowPoints` is '0' for a measured
 *  in-range vital and null for one with no breakdown row. */
function expectQuiet(key: CellKey, rowPoints: '0' | null) {
  // Paint first: this is the assertion the old cutoff classes fail.
  expect(painted(cell(key))).toEqual([])
  expect(chip(key)).toBeNull()
  expect(points(key)).toBe(rowPoints)
}

/* ─── Harness ─────────────────────────────────────────────────────────────── */

let consoleError: ReturnType<typeof vi.spyOn>

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
  await waitFor(() => expect(h.getLatestAssessmentForPatient).toHaveBeenCalled())
}

async function emit(event: string, payload: unknown) {
  await act(async () => { for (const fn of [...(h.handlers[event] || [])]) fn(payload) })
}

beforeEach(() => {
  vi.clearAllMocks()
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
  serveAssessment(null)
  consoleError = vi.spyOn(console, 'error')
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

afterEach(() => {
  // A chart that failed to load renders an error screen with no bar on it, and
  // every "nothing is coloured" assertion would pass against it.
  const failed = consoleError.mock.calls.some((c: unknown[]) => String(c[0]).includes('Failed to load patient:'))
  consoleError.mockRestore()
  expect(failed).toBe(false)
})

/* ══════════════════════════════════════════════════════════════════════════
   EACH CELL CARRIES THE POINTS ITS OWN ROW SCORED
   ══════════════════════════════════════════════════════════════════════════ */
describe('the bar reads the NEWS2 breakdown, not its own cutoffs', () => {
  const MIXED = [
    row('Temperature', 37.0, 0),
    row('Heart rate', 50, 1, 'Bradycardia (HR 50)'),
    row('Systolic BP', 90, 3, 'Moderate hypotension (SBP 90)'),
    row('SpO2', 95, 1, 'Mild hypoxia (SpO2 95%)'),
    row('Respiratory rate', 22, 2, 'Tachypnea (RR 22)'),
  ]
  const mixedChart = () => chart(
    vital({ heart_rate: 50, systolic_bp: 90, diastolic_bp: 60, spo2: 95, temperature: 37.0, respiratory_rate: 22 }),
    risk(MIXED),
  )

  it('SBP 90 scores 3: chip "+3" and the danger tone (the old bar left it plain)', async () => {
    h.detail = mixedChart()
    await openChart()
    expect(value('bp')).toBe('90/60')
    expectScored('bp', 3)
  })

  it('RR 22 scores 2: chip "+2" and the warn tone (the old bar waited for 25)', async () => {
    h.detail = mixedChart()
    await openChart()
    expect(value('rr')).toBe('22')
    expectScored('rr', 2)
  })

  it('HR 50 and SpO2 95 each score 1 (the old bar needed HR < 50 and SpO2 < 94)', async () => {
    h.detail = mixedChart()
    await openChart()
    expectScored('hr', 1)
    expectScored('spo2', 1)
    // Control in the same chart: Temp 37.0 is measured and scores nothing.
    expectQuiet('temp', '0')
  })

  it('RR 8 and Temp 34.9 each score 3 (the old bar had no low cutoff for either)', async () => {
    h.detail = chart(
      vital({ respiratory_rate: 8, temperature: 34.9 }),
      risk([
        row('Heart rate', 88, 0),
        row('Temperature', 34.9, 3, 'Hypothermia (34.9°C)'),
        row('SpO2', 96, 0),
        row('Respiratory rate', 8, 3, 'Moderate bradypnea (RR 8)'),
        row('Systolic BP', 124, 0),
      ]),
    )
    await openChart()
    expect(value('temp')).toBe('34.9')
    expectScored('rr', 3)
    expectScored('temp', 3)
  })

  it('HR 88 and SpO2 96 score 0: the cell records it, with no chip and no colour', async () => {
    h.detail = chart(
      vital({ heart_rate: 88, spo2: 96 }),
      risk([
        row('SpO2', 96, 0), row('Respiratory rate', 16, 0), row('Heart rate', 88, 0),
        row('Systolic BP', 124, 0), row('Temperature', 37.0, 0),
      ]),
    )
    await openChart()
    expectQuiet('hr', '0')
    expectQuiet('spo2', '0')
  })

  it('SBP 170 scores 0 and is not coloured (the old bar painted anything over 160)', async () => {
    h.detail = chart(
      vital({ systolic_bp: 170, diastolic_bp: 96 }),
      risk([
        row('Heart rate', 88, 0), row('Systolic BP', 170, 0), row('SpO2', 96, 0),
        row('Temperature', 37.0, 0), row('Respiratory rate', 16, 0),
      ]),
    )
    await openChart()
    expect(value('bp')).toBe('170/96')
    expectQuiet('bp', '0')
  })

  it('Scale 2 SpO2 90 is inside the target and scores 0: not coloured, and the label says Scale 2', async () => {
    h.detail = chart(
      vital({ spo2: 90 }),
      risk([
        row('Heart rate', 88, 0), row('SpO2 (Scale 2)', 90, 0), row('Systolic BP', 124, 0),
        row('Temperature', 37.0, 0), row('Respiratory rate', 16, 0),
      ], { scale2: true }),
    )
    await openChart()
    expect(value('spo2')).toBe('90')
    expectQuiet('spo2', '0')
    expect(label('spo2')).toMatch(/scored on Scale 2/)
  })

  it('Scale 2 SpO2 97 on oxygen scores 3 (the old bar saw a "normal" saturation)', async () => {
    h.detail = chart(
      vital({ spo2: 97, on_oxygen: 1 }),
      risk([
        row('Supplemental O2', true, 2, 'On supplemental oxygen'),
        row('Heart rate', 88, 0),
        row('SpO2 (Scale 2)', 97, 3, 'Over-oxygenation on O2 (97%, Scale 2)'),
        row('Systolic BP', 124, 0), row('Temperature', 37.0, 0), row('Respiratory rate', 16, 0),
      ], { scale2: true }),
    )
    await openChart()
    expectScored('spo2', 3)
    expect(label('spo2')).toMatch(/scored on Scale 2/)
  })

  it('unmeasured HR, SBP and SpO2 have no row: no attribute, no chip, no colour', async () => {
    // The engine writes no row for a vital nobody measured. The old bar
    // compared null against its cutoffs (null < 50 is true) and painted all
    // three amber.
    h.detail = chart(
      vital({ heart_rate: null, systolic_bp: null, diastolic_bp: null, spo2: null }),
      risk([row('Respiratory rate', 18, 0), row('Temperature', 37.0, 0)],
        { missing: ['Heart rate', 'SpO2', 'Systolic BP'] }),
    )
    await openChart()
    expect(value('hr')).toBe(NO_VALUE)
    expectQuiet('hr', null)
    expectQuiet('bp', null)
    expectQuiet('spo2', null)
    // Control: the measured ones still carry their (zero) rows.
    expectQuiet('rr', '0')
  })

  /* The case above only reaches a cutoff through null coercion. A fallback
     that ran the old cutoffs on MEASURED numbers alone would pass it, so here
     every number is measured and past an old cutoff, and the risk has no row
     for any of them: once with an empty breakdown, once with none at all. */
  it.each([
    ['an empty breakdown', { ...risk([]), breakdown: [] }],
    ['no breakdown field', { score: 0.45, level: 'critical', raw_news2: 9, drivers: [], scoring_model: 'NEWS2' }],
  ])('a measured number with no row is never painted (%s)', async (_shape, r) => {
    h.detail = chart(
      vital({ heart_rate: 130, systolic_bp: 170, diastolic_bp: 96, spo2: 90, temperature: 39.2, respiratory_rate: 28 }),
      r,
    )
    await openChart()
    // Control: the numbers are on the bar, so "nothing painted" is about them.
    expect([value('hr'), value('bp'), value('spo2'), value('temp'), value('rr')])
      .toEqual(['130', '170/96', '90', '39.2', '28'])
    for (const key of Object.keys(CELL) as CellKey[]) expectQuiet(key, null)
  })

  it('clamps a stray 4-point row to the "+3" level, as News2Drivers does', async () => {
    h.detail = chart(vital({ heart_rate: 135 }), risk([row('Heart rate', 135, 4, 'stray'), row('SpO2', 96, 0)]))
    await openChart()
    expectScored('hr', 3)
  })

  it('the no-vitals risk (no breakdown at all) colours nothing, and the badge says "not recorded"', async () => {
    h.detail = chart(null, {
      score: 0, level: 'stable', drivers: ['No vitals recorded'], scoring_model: 'NEWS2',
    })
    await openChart()
    for (const key of Object.keys(CELL) as CellKey[]) expectQuiet(key, null)
    const badge = document.querySelector('.command-bar__risk-pct') as HTMLElement
    expect(badge.textContent).toBe('STABLE')
    expect(badge.getAttribute('title')).toMatch(/^NEWS2 not recorded\. /)
    expect(document.body.textContent).not.toMatch(/NEWS2 undefined/)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   THE SAME FACT FOR A SCREEN READER
   ══════════════════════════════════════════════════════════════════════════ */
describe('the aria-label says what the chip shows', () => {
  it('adds "scores N NEWS2 point(s)" when a row exists, and nothing when it does not', async () => {
    h.detail = chart(
      vital({ heart_rate: 50, systolic_bp: 90, diastolic_bp: 60, temperature: null }),
      risk([
        row('Systolic BP', 90, 3, 'Moderate hypotension (SBP 90)'),
        row('Heart rate', 50, 1, 'Bradycardia (HR 50)'),
        row('SpO2', 96, 0), row('Respiratory rate', 16, 0),
      ]),
    )
    await openChart()
    // Singular for one point.
    expect(label('hr')).toMatch(/^Heart rate 50 bpm, scores 1 NEWS2 point(?!s)/)
    // BP names WHICH half scored: NEWS2 never scores the diastolic.
    expect(label('bp')).toMatch(/^Blood pressure 90 over 60 mmHg, systolic scores 3 NEWS2 points/)
    // Zero is said out loud, because measured-and-normal is a finding.
    expect(label('spo2')).toMatch(/^Oxygen saturation 96 percent, scores 0 NEWS2 points/)
    expect(label('spo2')).not.toMatch(/Scale 2/)
    // No row, no clause.
    expect(label('temp')).toMatch(/^Temperature not recorded degrees Celsius/)
    expect(label('temp')).not.toMatch(/NEWS2/)
  })

  it('exposes each cell as a labelled group, so the label is announced', async () => {
    h.detail = chart(vital(), risk([row('Heart rate', 88, 0)]))
    await openChart()
    expect(cell('hr').getAttribute('role')).toBe('group')
    expect(screen.getByRole('group', { name: /^Heart rate 88 bpm, scores 0 NEWS2 points/ })).toBe(cell('hr'))
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   BLOOD PRESSURE: ONE OF THE TWO NUMBERS IS SCORED
   ══════════════════════════════════════════════════════════════════════════ */
describe('the BP cell colours the systolic number only', () => {
  it('paints the systolic, never the diastolic, when the systolic scores', async () => {
    h.detail = chart(
      vital({ systolic_bp: 90, diastolic_bp: 40 }),
      risk([row('Systolic BP', 90, 3, 'Moderate hypotension (SBP 90)'), row('Heart rate', 88, 0)]),
    )
    await openChart()
    expect(value('bp')).toBe('90/40')
    const paint = painted(cell('bp'))
    expect(paint).toHaveLength(1)
    expect(paint[0].textContent).toBe('90')
  })

  it('paints nothing when only the diastolic is extreme', async () => {
    h.detail = chart(
      vital({ systolic_bp: 124, diastolic_bp: 40 }),
      risk([row('Systolic BP', 124, 0), row('Heart rate', 88, 0)]),
    )
    await openChart()
    expect(value('bp')).toBe('124/40')
    expectQuiet('bp', '0')
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   THE BAR'S CHIP IS THE BREAKDOWN'S CHIP
   ══════════════════════════════════════════════════════════════════════════ */
const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

/** The declarations of one single-selector rule, comments stripped, sorted. */
function declarations(css: string, selector: string): string[] | null {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = new RegExp(`(?:^|[\\s}])${esc}\\s*\\{([^}]*)\\}`).exec(clean)
  if (!m) return null
  return m[1].split(';').map(d => d.trim().replace(/\s+/g, ' ')).filter(Boolean).sort()
}

describe('the "+N" chip matches the News2Drivers chip', () => {
  it('picks the same level as News2Drivers for 1, 2 and 3, and shows no chip for 0', async () => {
    // News2Drivers' own pointsClass, read off a real render.
    const { unmount } = render(
      <News2Drivers
        id="parity"
        view={{
          source: 'chart', basis: 'parity', news2: 6, singleParam3: true, steady: [],
          rows: [0, 1, 2, 3].map(p => ({ key: `p${p}`, points: p, text: `row ${p}` })),
        }}
      />,
    )
    const driverLevel: Record<string, string> = {}
    for (const el of Array.from(document.querySelectorAll('.news2-drivers__points'))) {
      driverLevel[el.textContent!] = /news2-drivers__points--(\d)/.exec(el.className)![1]
    }
    expect(driverLevel).toEqual({ '+0': '0', '+1': '1', '+2': '2', '+3': '3' })
    unmount()

    h.detail = chart(
      vital({ heart_rate: 50, systolic_bp: 90, diastolic_bp: 60, respiratory_rate: 22 }),
      risk([
        row('Respiratory rate', 22, 2, 'Tachypnea (RR 22)'),
        row('Temperature', 37.0, 0),
        row('Systolic BP', 90, 3, 'Moderate hypotension (SBP 90)'),
        row('Heart rate', 50, 1, 'Bradycardia (HR 50)'),
      ]),
    )
    await openChart()
    const barLevel = (key: CellKey) => /command-bar__vital-points--(\d)/.exec(chip(key)?.className || '')?.[1]
    expect(barLevel('hr')).toBe(driverLevel['+1'])
    expect(barLevel('rr')).toBe(driverLevel['+2'])
    expect(barLevel('bp')).toBe(driverLevel['+3'])
    // A 0 is a chip in the breakdown list (behind its "scoring 0" toggle) but
    // is not a cue on the bar.
    expect(chip('temp')).toBeNull()
  })

  it('copies the News2Drivers chip declarations exactly, colour and fill in one rule', () => {
    const bar = read('../Vigil.css')
    const drivers = read('../../components/vigil/News2Drivers.css')
    for (const p of [1, 2, 3]) {
      const mine = declarations(bar, `.command-bar__vital-points--${p}`)
      expect(mine).toEqual(declarations(drivers, `.news2-drivers__points--${p}`))
      // The contrast gate only measures a rule that declares both.
      expect(mine!.some(d => d.startsWith('color:'))).toBe(true)
      expect(mine!.some(d => d.startsWith('background:'))).toBe(true)
    }
  })

  it('paints the scored number with status TEXT tokens and uses no opacity', () => {
    const bar = read('../Vigil.css')
    expect(declarations(bar, '.command-bar__vital-scored--danger')).toEqual(['color: var(--status-danger-text)'])
    expect(declarations(bar, '.command-bar__vital-scored--warn')).toEqual(['color: var(--status-warn-text)'])
    for (const sel of ['.command-bar__vital-points', '.command-bar__vital-head',
      '.command-bar__vital-points--1', '.command-bar__vital-points--2', '.command-bar__vital-points--3']) {
      expect(declarations(bar, sel)?.join(';') ?? '').not.toMatch(/opacity/)
    }
    // The cutoff modifiers are gone with the cutoffs.
    expect(declarations(bar, '.command-bar__vital--warn .command-bar__vital-value')).toBeNull()
    expect(declarations(bar, '.command-bar__vital--danger .command-bar__vital-value')).toBeNull()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   THE BADGE TOOLTIP CARRIES THE NOTES THE NUMBER CANNOT
   ══════════════════════════════════════════════════════════════════════════ */
describe('the bed badge', () => {
  it('keeps its text and puts the Scale 2 and single-parameter-3 notes in its title', async () => {
    h.detail = chart(
      vital({ spo2: 97, on_oxygen: 1 }),
      risk([
        row('SpO2 (Scale 2)', 97, 3, 'Over-oxygenation on O2 (97%, Scale 2)'),
        row('Supplemental O2', true, 2, 'On supplemental oxygen'),
        row('Heart rate', 88, 0),
      ], { scale2: true }),
    )
    await openChart()
    const badge = document.querySelector('.command-bar__risk-pct') as HTMLElement
    expect(badge.textContent).toBe('NEWS2 5 · ELEVATED')
    expect(badge.getAttribute('title')).toBe(
      'NEWS2 5 (elevated) · a single parameter scores 3 · SpO₂ Scale 2. '
      + 'NEWS2 aggregate (RCP NEWS2 chart), not a validated probability',
    )
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   TIER 1 ON SCREEN: THE BAR STILL DESCRIBES THE PATIENT NOW
   ══════════════════════════════════════════════════════════════════════════ */
describe('while Sage shows the alert\'s Tier 1 breakdown', () => {
  it('colours the bar from the live risk, and follows a live tick', async () => {
    serveAssessment(ASSESSMENT)
    h.detail = chart(
      vital({ systolic_bp: 65, diastolic_bp: 44 }),
      risk([row('Heart rate', 88, 0), row('Systolic BP', 65, 3, 'Severe hypotension (SBP 65)', true)]),
    )
    await openChart()
    await screen.findByText(MOVEMENT_SBP)
    // The chart's own breakdown stood down: one breakdown, and it is Tier 1.
    const blocks = document.querySelectorAll('.news2-drivers')
    expect(blocks).toHaveLength(1)
    expect(blocks[0].getAttribute('data-source')).toBe('tier1')

    expectScored('bp', 3)
    expectQuiet('hr', '0')

    // Three seconds later: pressure recovered, pulse up. Tier 1 is a snapshot
    // of the alert and stays; the bar is the patient now.
    await emit('vitals:update', {
      ...vital({ timestamp: '2026-10-02T08:00:03Z', heart_rate: 135, systolic_bp: 118, diastolic_bp: 76 }),
      risk: risk([row('Systolic BP', 118, 0), row('Heart rate', 135, 3, 'Moderate tachycardia (HR 135)')]),
    })
    expect(value('hr')).toBe('135')
    expectScored('hr', 3)
    expectQuiet('bp', '0')
    expect(screen.getByText(MOVEMENT_SBP)).toBeInTheDocument()
    expect(document.querySelectorAll('.news2-drivers')).toHaveLength(1)
  })
})
