/**
 * 'NOT ON WARD SINCE' ON THE VIGIL BOARD (adt-10), DISPLAY ONLY.
 *
 * With Beds and admissions on, GET /api/vigil/patients adds presence_status
 * and presence_since to each row; the row then says 'Not on ward since' in
 * local time. With the module off the rows carry neither and the board is
 * exactly as before. The score and the order are never changed by it.
 */
import { render, screen, waitFor } from '@testing-library/react'
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

import { formatClinicalTime } from '../../lib/datetime'

const SINCE = new Date(Date.now() - 45 * 60_000).toISOString()

function row(id: string, name: string, bed: string, raw: number, level: string,
             extra: Record<string, unknown> = {}) {
  const news2 = { score: Math.min(1, raw / 20), level, raw_news2: raw, raw_mews: raw,
    single_param_3: false, drivers: [], breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1,
    complete: true, missing_parameters: [], coverage_note: null }
  return {
    id, name, bed, mrn: `MRN-${id}`, ward: 'GM', department: 'General Medicine',
    admission_date: '2026-10-01', diagnosis: 'Pneumonia', age: 60, sex: 'M',
    news2, news2_at: '2026-10-09T08:00:00+00:00', risk_score: news2.score, risk_level: level,
    risk_drivers: [], attending_doctor_name: 'Dr Iyer', ...extra,
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

function serve(rows: unknown[]) {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => rows, headers: { get: () => String(rows.length) },
  })))
}

beforeEach(() => {
  for (const k of Object.keys(h.handlers)) delete h.handlers[k]
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function rowOf(name: string): HTMLElement {
  return screen.getByText(name).closest('.patient-row') as HTMLElement
}

describe('not on ward on the Vigil board', () => {
  it('says when the patient left, in local time, and leaves the score and order alone', async () => {
    serve([
      row('P1', 'Anita Rao', 'GM-01', 9, 'critical',
          { presence_status: 'on_leave', presence_since: SINCE }),
      row('P2', 'Rahul Menon', 'GM-02', 1, 'stable', { presence_status: null, presence_since: null }),
    ])
    renderPanel()
    await waitFor(() => expect(screen.getByText('Anita Rao')).toBeTruthy())
    const away = rowOf('Anita Rao')
    expect(away.textContent).toContain(`Not on ward since ${formatClinicalTime(SINCE)} (on leave)`)
    expect(rowOf('Rahul Menon').textContent).not.toContain('Not on ward')
    // The score is still shown, and the sicker patient is still first.
    expect(away.querySelector('.patient-row__score')?.textContent).toBe('9')
    const order = [...document.querySelectorAll('.patient-row__name')]
      .map(n => (n.firstChild?.textContent ?? '').trim())
    expect(order).toEqual(['Anita Rao', 'Rahul Menon'])
  })

  it('draws nothing when the rows carry no presence (Beds and admissions off)', async () => {
    serve([row('P1', 'Anita Rao', 'GM-01', 3, 'stable')])
    renderPanel()
    await waitFor(() => expect(screen.getByText('Anita Rao')).toBeTruthy())
    expect(document.querySelector('.adt-not-on-ward')).toBeNull()
  })

  it('marks an absconded patient', async () => {
    serve([row('P1', 'Anita Rao', 'GM-01', 3, 'stable',
               { presence_status: 'absconded', presence_since: SINCE })])
    renderPanel()
    await waitFor(() => expect(screen.getByText('Anita Rao')).toBeTruthy())
    expect(rowOf('Anita Rao').textContent).toContain('(absconded)')
  })
})
