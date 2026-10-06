/**
 * THE WARD BOARD ROW SHOWS NEWS2, AND KEEPS SHOWING THE LATEST ONE.
 *
 * The row's score cell used to render `{(p.risk_score * 100).toFixed(0)}%`.
 * `risk_score` is a 0-1 index of the NEWS2 aggregate, not a probability, so the
 * board's main number was a percentage that meant nothing; and a patient with
 * no risk row printed "0%", because `(null * 100).toFixed(0)` is "0". Ruled by
 * Samuel 2026-10-03: the row renders the shared News2Badge from the `news2`
 * object the list now carries (the latest vitals row, scored), the same way the
 * nurse board and the doctor home do.
 *
 * The live tick had two defects of its own:
 *   - ONE 2 s throttle shared by EVERY patient. A tick for bed 3 swallowed a
 *     tick for bed 7 that arrived inside the same window, so on a busy ward most
 *     rows only moved on the 30 s poll.
 *   - `newVital.risk?.score || p.risk_score` kept the stale score whenever the
 *     new one was 0: a patient who recovered to NEWS2 0 kept their old number.
 *
 * Every case here fails on the board as it stood before this file was added.
 */
import { render, screen, act, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => {
  const handlers: Record<string, Array<(p: unknown) => void>> = {}
  return {
    board: [] as unknown[],
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

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({})),
  getPatientDetail: vi.fn(() => new Promise(() => {})),
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
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

const T0 = '2026-10-03T08:00:00+00:00'
const T_LATER = '2026-10-03T08:00:05+00:00'
const T_EARLIER = '2026-10-03T07:00:00+00:00'

/** The `news2` object GET /api/vigil/patients carries (score_vitals_row's shape). */
function news2(raw: number, level: string, over: Record<string, unknown> = {}) {
  return {
    score: Math.min(1, raw / 20), level, raw_news2: raw, raw_mews: raw,
    single_param_3: false, drivers: [], breakdown: [], scoring_model: 'NEWS2',
    spo2_scale: 1, complete: true, missing_parameters: [], coverage_note: null,
    ...over,
  }
}

/** One board row, in the list route's shape. Legacy risk_* mirror `news2`. */
function boardRow(id: string, name: string, bed: string, n: ReturnType<typeof news2> | null) {
  return {
    id, name, bed, mrn: `MRN-${id}`, ward: 'GM', department: 'General Medicine',
    admission_date: '2026-10-01', diagnosis: 'Pneumonia', age: 60, sex: 'M',
    news2: n, news2_at: n ? T0 : null,
    risk_score: n ? n.score : null, risk_level: n ? n.level : null,
    risk_drivers: n ? n.drivers : [], attending_doctor_name: 'Dr Iyer',
  }
}

/** One `vitals:update` payload as bot_service / ingestion_service emit it. */
function tick(patientId: string, timestamp: string, risk: ReturnType<typeof news2>) {
  return {
    patient_id: patientId, timestamp, heart_rate: 80, systolic_bp: 120, diastolic_bp: 75,
    spo2: 97, temperature: 37, respiratory_rate: 16, consciousness: 'alert', on_oxygen: false,
    risk, source: 'simulated',
  }
}

function renderPanel() {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
}

async function emit(payload: unknown) {
  await act(async () => { for (const fn of [...(h.handlers['vitals:update'] || [])]) fn(payload) })
}

function rowOf(name: string): HTMLElement {
  return screen.getByText(name).closest('.patient-row') as HTMLElement
}

/** The score cell as a reader gets it: the visible text and the spoken label. */
function scoreOf(name: string) {
  const cell = rowOf(name).querySelector('.patient-row__score') as HTMLElement
  const badge = cell.querySelector('[role="img"]')
  return { text: cell.textContent, label: badge?.getAttribute('aria-label') ?? null }
}

function boardOrder(): string[] {
  return [...document.querySelectorAll('.patient-row__name')]
    .map(n => (n.firstChild?.textContent ?? '').trim())
}

beforeEach(() => {
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
  h.board = [
    boardRow('P1', 'Anita Rao', 'GM-01', news2(9, 'critical', { score: 0.45 })),
    boardRow('P2', 'Rahul Menon', 'GM-02', news2(1, 'stable')),
    boardRow('P3', 'Kiran Das', 'GM-03', null),
  ]
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => h.board,
    headers: { get: () => String(h.board.length) },
  })))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function boardReady() {
  renderPanel()
  await waitFor(() => expect(screen.getByText('Anita Rao')).toBeTruthy())
}

describe('the board row shows NEWS2, never a percentage', () => {
  it('renders the aggregate and its band, with the sentence in the label', async () => {
    await boardReady()
    expect(scoreOf('Anita Rao')).toEqual({ text: '9', label: 'NEWS2 9 (critical)' })
    expect(scoreOf('Rahul Menon')).toEqual({ text: '1', label: 'NEWS2 1 (stable)' })
    for (const row of document.querySelectorAll('.patient-row')) {
      expect(row.textContent).not.toContain('%')
    }
  })

  it('says "not recorded" for a patient with no observation, never "0%"', async () => {
    await boardReady()
    const none = scoreOf('Kiran Das')
    expect(none.text).toBe('—')
    expect(none.label).toBe('NEWS2 not recorded')
    expect(rowOf('Kiran Das').textContent).not.toContain('0%')
  })

  it('marks a score from an incomplete set as a minimum', async () => {
    h.board[1] = boardRow('P2', 'Rahul Menon', 'GM-02', news2(5, 'elevated', {
      complete: false, missing_parameters: ['Consciousness (ACVPU)'],
    }))
    await boardReady()
    const partial = scoreOf('Rahul Menon')
    expect(partial.text).toBe('≥5')
    // Same sentence as the server twin (news2_text.py) prints in alerts.
    expect(partial.label).toBe('NEWS2 5 (elevated) · incomplete set, minimum')
  })
})

describe('the board orders by the number it shows', () => {
  it('critical first, then NEWS2 high to low, elevated over stable at a tie, not recorded last', async () => {
    h.board = [
      boardRow('PA', 'Nothing Recorded', 'GM-10', null),
      // Same aggregate, same 0-1 index. Listed stable-first so a sort on the
      // index alone (which ties them) leaves the elevated patient second.
      boardRow('PC', 'Spread Three', 'GM-11', news2(3, 'stable')),
      boardRow('PB', 'Single Three', 'GM-12', news2(3, 'elevated', { single_param_3: true })),
      boardRow('PE', 'All Zero', 'GM-13', news2(0, 'stable')),
      boardRow('PD', 'Peri Arrest', 'GM-14', news2(9, 'critical')),
    ]
    renderPanel()
    await waitFor(() => expect(screen.getByText('Peri Arrest')).toBeTruthy())
    expect(boardOrder()).toEqual(['Peri Arrest', 'Single Three', 'Spread Three', 'All Zero', 'Nothing Recorded'])
  })
})

describe('the live tick', () => {
  it('replaces the cell when the new score is 0 (it used to keep the stale one)', async () => {
    await boardReady()
    await emit(tick('P1', T_LATER, news2(0, 'stable')))
    await waitFor(() => expect(scoreOf('Anita Rao')).toEqual({ text: '0', label: 'NEWS2 0 (stable)' }))
    expect(rowOf('Anita Rao').querySelector('.patient-row__risk--stable')).not.toBeNull()
  })

  it('lands a tick for every patient, not just the first one in a 2 s window', async () => {
    await boardReady()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    await emit(tick('P1', T_LATER, news2(6, 'elevated')))
    await emit(tick('P2', T_LATER, news2(7, 'critical')))
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(scoreOf('Anita Rao').label).toBe('NEWS2 6 (elevated)')
    expect(scoreOf('Rahul Menon').label).toBe('NEWS2 7 (critical)')
  })

  it('keeps the newer of two ticks for one patient inside one window, whatever the arrival order', async () => {
    await boardReady()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    // The first tick lands at once and opens the window; the next two wait in it.
    await emit(tick('P1', T_LATER, news2(6, 'elevated')))
    await emit(tick('P1', '2026-10-03T08:00:15+00:00', news2(8, 'critical')))
    await emit(tick('P1', '2026-10-03T08:00:10+00:00', news2(3, 'stable')))
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(scoreOf('Anita Rao').label).toBe('NEWS2 8 (critical)')
  })

  it('ignores a tick older than the observation the row already shows', async () => {
    await boardReady()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    // A backfilled reading from an hour earlier must not paint over the
    // newer set the list scored.
    await emit(tick('P1', T_EARLIER, news2(2, 'stable')))
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(scoreOf('Anita Rao')).toEqual({ text: '9', label: 'NEWS2 9 (critical)' })
  })
})

describe('the board row shows the readings behind the score', () => {
  it('prints the two worst readings beside the diagnosis, and none at NEWS2 0', async () => {
    h.board = [
      boardRow('P1', 'Anita Rao', 'GM-01', news2(9, 'critical', {
        breakdown: [
          { parameter: 'Respiratory rate', value: 37, points: 3, note: 'Severe tachypnea (RR 37)', severe: true },
          { parameter: 'SpO2', value: 84, points: 3, note: 'Severe hypoxia (SpO2 84%)', severe: true },
          { parameter: 'Heart rate', value: 118, points: 2, note: 'Tachycardia (HR 118)', severe: false },
          { parameter: 'Systolic BP', value: 130, points: 0, note: null, severe: false },
        ],
      })),
      boardRow('P2', 'Rahul Menon', 'GM-02', news2(0, 'stable', {
        breakdown: [{ parameter: 'Heart rate', value: 72, points: 0, note: null, severe: false }],
      })),
    ]
    await boardReady()
    const readings = rowOf('Anita Rao').querySelector('.patient-row__readings')
    expect(readings?.textContent).toBe('RR 37 · SpO2 84%')
    expect(rowOf('Rahul Menon').querySelector('.patient-row__readings')).toBeNull()
  })
})
