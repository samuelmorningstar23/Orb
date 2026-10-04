/**
 * ONE OBSERVATION ON THE CHART: THE BAR'S NUMBERS AND THE SCORE ARE ONE PAIR.
 *
 * The chart held the latest vital set in one state (`latestVital`, replaced on
 * every socket tick) and the NEWS2 that scored it in another (`data.risk`,
 * replaced only when a tick also cleared the 5 s trend throttle). So for up to
 * five seconds after every tick the command bar showed the NEW numbers beside
 * the OLD score: SBP 88 in the bar, "NEWS2 0 · STABLE" on the badge, "Why
 * NEWS2 0" in the panel under it. A late chart reload could also land after a
 * newer tick and step the bar back to an observation that was already
 * superseded, and a strictly older (backdated) tick stepped it back too.
 *
 * The chart now keeps the newest observation as ONE {vital, risk} pair,
 * replaced only as a whole, and every reader of "the patient now" (bar, badge,
 * gauge, drivers, breakdown, discharge modal) reads that pair. The trend array
 * keeps its 5 s throttle; it is a sparkline, not the current observation.
 *
 * Timestamps are in the payloads, so no fake timers are needed.
 */
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openPatient } from '../../lib/orbBus'

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void }
/** What getPatientDetail answers, as far as these tests read it. */
type ChartDetail = { vitals: Record<string, unknown>[]; risk: unknown } & Record<string, unknown>
function defer<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

const h = vi.hoisted(() => {
  const handlers: Record<string, Array<(p: unknown) => void>> = {}
  return {
    details: {} as Record<string, ChartDetail>,
    /** How the next getPatientDetail call answers. Tests swap it to hold a
     *  reload open while a tick arrives. */
    respond: null as null | ((pid: string) => Promise<unknown>),
    handlers,
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
  getPatientDetail: vi.fn((pid: string) => (h.respond ? h.respond(pid) : Promise.resolve(h.details[pid]))),
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

/* Sage's alert block is a reader of "the patient now" too (its risk level turns
   on the critical "watching" poll). The real component still renders; this
   only records the props it was given. */
const sage = vi.hoisted(() => ({ props: [] as Array<{ riskLevel?: string | null }> }))
vi.mock('../../components/sage/SageAlertAssessment', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../components/sage/SageAlertAssessment')>()
  const { createElement } = await import('react')
  type Props = Parameters<typeof real.default>[0]
  return { ...real, default: (p: Props) => { sage.props.push(p); return createElement(real.default, p) } }
})

import VigilPanel from './VigilPanel'
import { NO_VALUE } from '../../lib/vitals'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

/* ─── Fixtures in the server's shapes ─────────────────────────────────────── */

type Row = { parameter: string; value: unknown; points: number; note: string | null; severe: boolean }
const row = (parameter: string, value: unknown, points: number, note: string | null = null): Row =>
  ({ parameter, value, points, note, severe: false })

/** The risk envelope around hand-written rows (sums them in the engine's bands). */
function risk(rows: Row[]) {
  const raw = rows.reduce((n, r) => n + r.points, 0)
  const any3 = rows.some(r => r.points === 3)
  const level = raw >= 7 ? 'critical' : raw >= 5 || any3 ? 'elevated' : 'stable'
  return {
    score: Math.min(1, raw / 20), level, raw_news2: raw, raw_mews: raw, single_param_3: any3,
    drivers: rows.filter(r => r.points > 0).map(r => r.note), complete: true,
    missing_parameters: [], coverage_note: null,
    breakdown: rows, scoring_model: 'NEWS2', spo2_scale: 1,
  }
}

type Vital = Record<string, unknown>
const vital = (patientId: string, timestamp: string, over: Vital = {}): Vital => ({
  patient_id: patientId, timestamp,
  heart_rate: 72, systolic_bp: 124, diastolic_bp: 78, spo2: 97, temperature: 37.0, respiratory_rate: 16,
  consciousness: 'alert', on_oxygen: false,
  ...over,
})

/** Rows for a set where only `scored` contributes. */
const calmRows = (v: Vital, scored: Row[] = []) => {
  const taken = new Set(scored.map(r => r.parameter))
  const all: Row[] = [
    row('Heart rate', v.heart_rate, 0), row('Systolic BP', v.systolic_bp, 0), row('SpO2', v.spo2, 0),
    row('Temperature', v.temperature, 0), row('Respiratory rate', v.respiratory_rate, 0),
  ].filter(r => !taken.has(r.parameter))
  // Unsorted: the scoring rows go in the middle.
  return [...all.slice(0, 2), ...scored, ...all.slice(2)]
}

/** One `vitals:update` payload, as bot_service / ingestion_service emit it. */
const tick = (patientId: string, timestamp: string, over: Vital = {}, scored: Row[] = []) => {
  const v = vital(patientId, timestamp, over)
  return { ...v, risk: risk(calmRows(v, scored)), source: 'manual' }
}

const T0 = '2026-10-02T08:00:00Z'
const T_PREV = '2026-10-02T07:55:00Z'
const AT = (seconds: number) => new Date(Date.parse(T0) + seconds * 1000).toISOString()

const P007 = {
  patient_id: 'P007', name: 'Rahul Menon', bed: '7', mrn: 'MRN-P007', diagnosis: 'Pneumonia',
  admission_date: '2026-09-30', age: 71, sex: 'M', comorbidities: [],
}
const P008 = {
  patient_id: 'P008', name: 'Anita Rao', bed: '8', mrn: 'MRN-P008', diagnosis: 'Cellulitis',
  admission_date: '2026-09-29', age: 54, sex: 'F', comorbidities: [],
}

function detailOf(patient: typeof P007, vitals: Vital[]) {
  const latest = vitals[0]
  return {
    patient, vitals, medications: [], notes: [], reports: [], labs: [], alerts: [], lens_analyses: [],
    risk: risk(calmRows(latest)), trend: { trend: 'insufficient_data' },
  }
}

/* The chart as opened: two sets five minutes apart, NEWS2 0. */
const BASE = vital('P007', T0)
const PREV = vital('P007', T_PREV, { heart_rate: 70 })
const chartAtT0 = () => detailOf(P007, [BASE, PREV])

/* The tick the whole file turns on: three seconds later, SBP 88 scores 3. */
const HYPO = (at: string) => tick('P007', at, { systolic_bp: 88, diastolic_bp: 56 },
  [row('Systolic BP', 88, 3, 'Moderate hypotension (SBP 88)')])

/* ─── Reading the chart ───────────────────────────────────────────────────── */

const cell = (prefix: string) =>
  document.querySelector(`.command-bar__vital[aria-label^="${prefix}"]`) as HTMLElement
const text = (sel: string) => document.querySelector(sel)?.textContent ?? null

/** Everything on screen that states "the patient now". */
function nowOnScreen() {
  return {
    hr: cell('Heart rate').querySelector('.command-bar__vital-value')!.textContent,
    bp: cell('Blood pressure').querySelector('.command-bar__vital-value')!.textContent,
    badge: text('.command-bar__risk-pct'),
    why: text('.news2-drivers__title'),
    gauge: text('.risk-gauge__pct'),
    drivers: Array.from(document.querySelectorAll('.detail__driver')).map(d => d.textContent),
  }
}

const AT_T0 = {
  hr: '72', bp: '124/78', badge: 'NEWS2 0 · STABLE', why: 'Why NEWS2 0', gauge: '0', drivers: [],
}
const AFTER_HYPO = {
  hr: '72', bp: '88/56', badge: 'NEWS2 3 · ELEVATED', why: 'Why NEWS2 3', gauge: '3',
  drivers: ['Moderate hypotension (SBP 88)'],
}

const bpPoints = () => cell('Blood pressure').getAttribute('data-news2-points')

/** Points drawn in the heart-rate sparkline: how many sets the trend array holds. */
function trendPoints(): number {
  const line = cell('Heart rate').querySelector('.command-bar__vital-tooltip polyline')
  return line ? line.getAttribute('points')!.trim().split(/\s+/).length : 0
}

/* ─── Harness ─────────────────────────────────────────────────────────────── */

let consoleError: ReturnType<typeof vi.spyOn>
let loadFailureExpected = false

function renderPanel() {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
}

async function openChart(id = 'P007', name = 'Rahul Menon') {
  await act(async () => { openPatient(id, name) })
  await screen.findByText(name)
  await waitFor(() => expect(cell('Heart rate')).not.toBeNull())
}

async function emit(payload: unknown) {
  await act(async () => { for (const fn of [...(h.handlers['vitals:update'] || [])]) fn(payload) })
}

/** Holds the next chart read open until the test resolves it. */
function holdNextRead() {
  const d = defer<unknown>()
  h.respond = () => { h.respond = null; return d.promise }
  return d
}

/** Drops the connection: the chart is then served as the offline snapshot. */
async function goOffline() {
  Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false })
  await act(async () => { window.dispatchEvent(new Event('offline')) })
}

/** A patient with nothing charted yet: the risk the chart route returns then. */
const NO_VITALS_RISK = { score: 0, level: 'stable', drivers: ['No vitals recorded'], scoring_model: 'NEWS2' }
const emptyChart = () => ({ ...detailOf(P007, [BASE]), vitals: [], risk: NO_VITALS_RISK })
const NOTHING_RECORDED = {
  hr: NO_VALUE, bp: `${NO_VALUE}/${NO_VALUE}`, badge: 'STABLE', why: null, gauge: NO_VALUE,
  drivers: ['No vitals recorded'],
}

beforeEach(() => {
  vi.clearAllMocks()
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
  h.details = { P007: chartAtT0(), P008: detailOf(P008, [vital('P008', T0, { heart_rate: 64, systolic_bp: 132, diastolic_bp: 84, respiratory_rate: 11 })]) }
  // P008 has a respiratory rate of 11: one point.
  h.details.P008.risk = risk(calmRows(h.details.P008.vitals[0], [row('Respiratory rate', 11, 1, 'Low RR (11)')]))
  h.respond = null
  sage.props.length = 0
  loadFailureExpected = false
  consoleError = vi.spyOn(console, 'error')
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
  renderPanel()
})

afterEach(() => {
  // goOffline shadows jsdom's navigator.onLine; drop the shadow so the next
  // chart opens online.
  delete (navigator as { onLine?: boolean }).onLine
  const failed = consoleError.mock.calls.some(c => String(c[0]).includes('Failed to load patient:'))
  consoleError.mockRestore()
  expect(failed).toBe(loadFailureExpected)
})

/* ══════════════════════════════════════════════════════════════════════════
   (a) A NEWER TICK MOVES EVERYTHING TOGETHER
   ══════════════════════════════════════════════════════════════════════════ */
describe('a newer tick inside the 5 s trend window', () => {
  it('moves the bar, its points, the badge, the gauge and the breakdown together', async () => {
    await openChart()
    // Control: the chart opened on the T0 pair.
    expect(nowOnScreen()).toEqual(AT_T0)

    await emit(HYPO(AT(3)))

    // THE DEFECT: the bar took SBP 88 while the badge, gauge and "Why NEWS2"
    // panel stayed on the throttled NEWS2 0 for up to five seconds.
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
    expect(bpPoints()).toBe('3')
  })

  it('carries the same pair into the discharge modal', async () => {
    await openChart()
    await emit(HYPO(AT(3)))
    fireEvent.click(screen.getByRole('button', { name: 'Discharge' }))
    const line = (await screen.findByText('Current NEWS2:')).parentElement!
    expect(line.textContent).toBe('Current NEWS2: elevated (score 3)')
  })

  it('carries the same set into Print record\'s latest vitals', async () => {
    vi.stubGlobal('print', vi.fn())
    await openChart()
    await emit(HYPO(AT(3)))
    fireEvent.click(screen.getByRole('button', { name: 'Print record' }))
    const line = (await screen.findByText('LATEST RECORDED VITALS:')).parentElement!
    expect(line.textContent).toMatch(/BP 88\/56 mmHg/)
  })

  it('gives Sage\'s alert block the same risk level', async () => {
    await openChart()
    // Control: the chart opened stable.
    expect(sage.props.at(-1)?.riskLevel).toBe('stable')
    await emit(tick('P007', AT(3), { heart_rate: 135, systolic_bp: 85, respiratory_rate: 26 }, [
      row('Heart rate', 135, 3, 'Moderate tachycardia (HR 135)'),
      row('Systolic BP', 85, 3, 'Severe hypotension (SBP 85)'),
      row('Respiratory rate', 26, 3, 'Tachypnea (RR 26)'),
    ]))
    // Critical is what turns on Sage's "watching" poll: it must not wait for
    // the trend array's throttle.
    expect(sage.props.at(-1)?.riskLevel).toBe('critical')
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   (b) (c) (d) WHICH TICKS MAY REPLACE THE PAIR
   ══════════════════════════════════════════════════════════════════════════ */
describe('which ticks replace the pair', () => {
  it('(b) a correction at the SAME timestamp replaces everything (the later insert wins)', async () => {
    await openChart()
    expect(nowOnScreen()).toEqual(AT_T0)
    // Same clock time as the set on screen: the server's id DESC tiebreak
    // makes this the newer row, and it arrived later.
    await emit(HYPO(T0))
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
    expect(bpPoints()).toBe('3')
  })

  it('(c) a strictly OLDER tick changes nothing (it used to step the bar back)', async () => {
    await openChart()
    await emit(tick('P007', '2026-10-02T07:30:00Z', { heart_rate: 135 },
      [row('Heart rate', 135, 3, 'Moderate tachycardia (HR 135)')]))
    expect(nowOnScreen()).toEqual(AT_T0)
    expect(bpPoints()).toBe('0')
  })

  it('(d) a tick with no risk changes nothing, neither the bar nor the trend', async () => {
    await openChart()
    expect(trendPoints()).toBe(2)

    const bare: Vital = tick('P007', AT(3), { heart_rate: 135 })
    delete bare.risk
    await emit(bare)
    expect(nowOnScreen()).toEqual(AT_T0)

    // Past the 5 s throttle too: a set with no score is not appended either.
    await emit({ ...tick('P007', AT(10), { heart_rate: 140 }), risk: null })
    expect(nowOnScreen()).toEqual(AT_T0)
    expect(trendPoints()).toBe(2)
  })

  /* A set that cannot be dated is never a reason to keep an older one: only a
     tick PROVABLY older than the pair on screen is refused. */
  it('a tick whose timestamp does not parse is accepted', async () => {
    await openChart()
    await emit({ ...HYPO(AT(3)), timestamp: 'not a timestamp' })
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
  })

  it('a tick with no timestamp at all is accepted', async () => {
    await openChart()
    const noStamp: Record<string, unknown> = { ...HYPO(AT(3)) }
    delete noStamp.timestamp
    expect('timestamp' in noStamp).toBe(false)
    await emit(noStamp)
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
  })

  it('(g) a tick for another patient changes nothing', async () => {
    await openChart()
    await emit(tick('P999', AT(3), { heart_rate: 150, systolic_bp: 80 },
      [row('Heart rate', 150, 3, 'Severe tachycardia (HR 150)'), row('Systolic BP', 80, 3, 'Severe hypotension (SBP 80)')]))
    expect(nowOnScreen()).toEqual(AT_T0)
    expect(trendPoints()).toBe(2)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   (e) A RELOAD THAT LANDS AFTER A NEWER TICK DOES NOT STEP BACK
   ══════════════════════════════════════════════════════════════════════════ */
describe('a chart reload racing a tick', () => {
  it('(e) a reconnect reload that resolves after a newer tick keeps the tick', async () => {
    await openChart()
    // The reload's server read happened before the tick was written, so it
    // answers with the T0 chart.
    const read = holdNextRead()
    await act(async () => { window.dispatchEvent(new Event('online')) })
    await emit(HYPO(AT(10)))
    await act(async () => { read.resolve(chartAtT0()) })
    await waitFor(() => expect(cell('Heart rate')).not.toBeNull())

    expect(nowOnScreen()).toEqual(AFTER_HYPO)
    expect(bpPoints()).toBe('3')
  })

  it('(e) Retry after a failed load, resolving after a tick, keeps the tick', async () => {
    loadFailureExpected = true
    h.respond = () => { h.respond = null; return Promise.reject(Object.assign(new Error('Bad gateway'), { status: 502 })) }
    await act(async () => { openPatient('P007', 'Rahul Menon') })
    const retry = await screen.findByRole('button', { name: 'Retry' })

    const read = holdNextRead()
    fireEvent.click(retry)
    await emit(HYPO(AT(10)))
    await act(async () => { read.resolve(chartAtT0()) })
    await screen.findByText('Rahul Menon')

    expect(nowOnScreen()).toEqual(AFTER_HYPO)
  })

  it('(e) a reload with NO stored set, resolving after the first tick, keeps the tick', async () => {
    // A newly admitted patient: nothing charted yet.
    h.details.P007 = emptyChart()
    await openChart()
    // Control: the empty chart says so, and an empty answer is shown as one.
    expect(nowOnScreen()).toEqual(NOTHING_RECORDED)

    // The reload's read ran before the patient's first set was written. (The
    // chart shows its loading screen while the read is out, so there is no
    // bar to read until it resolves.)
    const read = holdNextRead()
    await act(async () => { window.dispatchEvent(new Event('online')) })
    await emit(HYPO(AT(10)))
    await act(async () => { read.resolve(emptyChart()) })
    await waitFor(() => expect(cell('Heart rate')).not.toBeNull())

    // THE DEFECT: an empty answer has no set to date, so it always won, and
    // the first observation (SBP 88, NEWS2 3) went back to "No vitals
    // recorded" until the next tick, hours away on the manual-obs lane.
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
    expect(bpPoints()).toBe('3')
  })

  it('control: a reload carrying the SAME timestamp as the tick wins (the server row is authoritative)', async () => {
    await openChart()
    await emit(HYPO(AT(10)))
    expect(nowOnScreen()).toEqual(AFTER_HYPO)

    // The stored set at T0+10s was corrected (SBP 96: 2 points) after the tick.
    const corrected = vital('P007', AT(10), { systolic_bp: 96, diastolic_bp: 60 })
    const fresh = detailOf(P007, [corrected, BASE, PREV])
    fresh.risk = risk(calmRows(corrected, [row('Systolic BP', 96, 2, 'Hypotension (SBP 96)')]))
    const read = holdNextRead()
    await act(async () => { window.dispatchEvent(new Event('online')) })
    await act(async () => { read.resolve(fresh) })
    await waitFor(() => expect(cell('Heart rate')).not.toBeNull())

    expect(nowOnScreen()).toEqual({
      hr: '72', bp: '96/60', badge: 'NEWS2 2 · STABLE', why: 'Why NEWS2 2', gauge: '2',
      drivers: ['Hypotension (SBP 96)'],
    })
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   (f) THE TREND ARRAY KEEPS ITS THROTTLE
   ══════════════════════════════════════════════════════════════════════════ */
describe('the sparkline array', () => {
  it('(f) control: still appends at most one set per 5 s', async () => {
    await openChart()
    expect(trendPoints()).toBe(2)
    await emit(tick('P007', AT(3), { heart_rate: 90 }))
    expect(trendPoints()).toBe(2)
    await emit(tick('P007', AT(10), { heart_rate: 95 }, [row('Heart rate', 95, 1, 'Mild tachycardia (HR 95)')]))
    expect(trendPoints()).toBe(3)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   OFFLINE: THE SNAPSHOT IS ITS OWN MATCHING PAIR
   A chart served from the device reads the snapshot's newest set and the score
   stored beside it (the 2026-10-02 derivation). The live pair is not shown
   under the offline banner. Whether it should be, so a throttled tick is not
   hidden for the whole offline period, is an open question for Samuel; if he
   rules that way, the second case below flips.
   ══════════════════════════════════════════════════════════════════════════ */
describe('going offline', () => {
  it('after an appended tick, shows that tick with ITS score', async () => {
    await openChart()
    // Past the 5 s throttle: appended to the trend array, and the stored
    // score must move with it or the snapshot pairs a new set with an old one.
    await emit(HYPO(AT(10)))
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
    await goOffline()
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
  })

  it('after a throttled tick, shows the snapshot\'s own pair, not the live one', async () => {
    await openChart()
    await emit(HYPO(AT(3)))
    expect(nowOnScreen()).toEqual(AFTER_HYPO)
    await goOffline()
    expect(nowOnScreen()).toEqual(AT_T0)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   A BARE SQLITE STAMP IS UTC
   A reloaded row can carry "2026-10-02 08:00:00" (UTC, no marker), which
   Date.parse and new Date() read as LOCAL time. In India that dates the stored
   set 5.5 h early, so a tick hours OLDER than it looks newer. Pinned to IST,
   because in a UTC zone the two readings agree and nothing here could fail.
   ══════════════════════════════════════════════════════════════════════════ */
describe('ordering a tick against a bare SQLite stamp', () => {
  let tz: string | undefined
  beforeEach(() => {
    tz = process.env.TZ
    process.env.TZ = 'Asia/Kolkata'
    // Precondition: the pin took. Without it both cases pass vacuously.
    expect(new Date(2026, 9, 2, 8).getTimezoneOffset()).toBe(-330)
    h.details.P007 = detailOf(P007, [vital('P007', '2026-10-02 08:00:00'), PREV])
  })
  afterEach(() => {
    if (tz === undefined) delete process.env.TZ
    else process.env.TZ = tz
  })

  /* Half an hour OLDER than the stored set, written with an explicit Z. */
  const LATE_OLD = () => tick('P007', '2026-10-02T07:30:00Z', { heart_rate: 135 },
    [row('Heart rate', 135, 3, 'Moderate tachycardia (HR 135)')])

  it('the bar refuses a tick older than the stored set', async () => {
    await openChart()
    await emit(LATE_OLD())
    expect(nowOnScreen()).toEqual(AT_T0)
  })

  it('the trend array refuses it too, so the offline snapshot cannot step back', async () => {
    await openChart()
    expect(trendPoints()).toBe(2)
    await emit(LATE_OLD())
    // THE DEFECT: the throttle read the stored stamp with new Date(), saw a
    // gap of +5 h, appended the old tick as the newest point and stored its
    // NEWS2 3 as the chart's score.
    expect(trendPoints()).toBe(2)
    await goOffline()
    expect(nowOnScreen()).toEqual(AT_T0)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   (h) A DIFFERENT PATIENT IS A DIFFERENT PAIR
   ══════════════════════════════════════════════════════════════════════════ */
describe('switching patient', () => {
  it('(h) never shows the previous patient\'s pair', async () => {
    await openChart()
    await emit(HYPO(AT(3)))
    // The tick reached P007's bar (the pairing itself is case (a)).
    expect(nowOnScreen().bp).toBe('88/56')

    const read = holdNextRead()
    await act(async () => { openPatient('P008', 'Anita Rao') })
    // While P008's chart is still loading, P007 is not on screen in any form.
    expect(screen.queryByText('Rahul Menon')).toBeNull()
    expect(document.querySelector('.command-bar__risk-pct')).toBeNull()

    await act(async () => { read.resolve(h.details.P008) })
    await screen.findByText('Anita Rao')
    const p008 = {
      hr: '64', bp: '132/84', badge: 'NEWS2 1 · STABLE', why: 'Why NEWS2 1', gauge: '1',
      drivers: ['Low RR (11)'],
    }
    expect(nowOnScreen()).toEqual(p008)

    // A late P007 tick lands on P008's chart and is ignored.
    await emit(HYPO(AT(6)))
    expect(nowOnScreen()).toEqual(p008)
  })
})
