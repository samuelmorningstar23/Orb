/**
 * The ward board with scoring switched off (ORB_SCORING_ENABLED, the Orb Ward
 * Record). The server sends no NEWS2 and orders the board by ward, then bed
 * (vigil_service.ward_bed_order). The board used to re-sort those rows by the
 * score they do not carry, which left them in patient id order, and it drew a
 * "no observations recorded" dash for patients whose sets are on file.
 */
import { render, screen, act, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  board: [] as unknown[],
  socket: { on: () => {}, off: () => {}, emit: () => {} },
}))

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({})),
  getPatientDetail: vi.fn(() => new Promise(() => {})),
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: () => false,
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
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'

/** A board row as GET /api/vigil/patients sends it with scoring switched off. */
function row(id: string, name: string, ward: string, bed: string) {
  return {
    id, name, bed, ward, mrn: `MRN-${id}`, department: 'General Medicine',
    admission_date: '2026-10-01', diagnosis: 'Pneumonia', age: 60, sex: 'M',
    news2: null, news2_at: null, risk_score: null, risk_level: null, risk_drivers: [],
    attending_doctor_name: 'Dr Iyer',
  }
}

function names(): string[] {
  return [...document.querySelectorAll('.patient-row__name')]
    .map(n => (n.firstChild?.textContent ?? '').trim())
}

beforeEach(() => {
  // Listed in id order, which is NOT ward then bed: a client sort by id would
  // keep it, a client that keeps the server's ward and bed order would not.
  h.board = [
    row('P1', 'Ward Two Bed One', 'GM-2', 'B-1'),
    row('P2', 'Ward One Bed Ten', 'GM-1', 'B-10'),
    row('P3', 'Ward One Bed Two', 'GM-1', 'B-2'),
    row('P4', 'No Bed Yet', 'GM-1', ''),
  ]
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => h.board,
    headers: { get: () => String(h.board.length) },
  })))
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.unstubAllGlobals()
})

function renderPanel() {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
}

describe('the ward board with scoring switched off', () => {
  it('lists patients by ward, then bed, numbers in number order', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    renderPanel()
    await waitFor(() => expect(screen.getByText('Ward One Bed Two')).toBeTruthy())
    expect(names()).toEqual(['Ward One Bed Two', 'Ward One Bed Ten', 'No Bed Yet', 'Ward Two Bed One'])
  })

  it('draws no score badge, so no row claims nothing was recorded', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    renderPanel()
    await waitFor(() => expect(screen.getByText('Ward One Bed Two')).toBeTruthy())
    for (const cell of document.querySelectorAll('.patient-row__score')) {
      expect(cell.querySelector('[role="img"]')).toBeNull()
      expect(cell.textContent).toBe('')
    }
  })

  it('shows no readings, no band stripe and floats nobody even when a row carries a score', async () => {
    // A row that arrived scored (a feed left running, or a page loaded just
    // before the switch was turned off) must still read as unscored.
    h.board = [
      row('P1', 'Ward One Bed One', 'GM-1', 'B-1'),
      {
        ...row('P2', 'Ward Two Bed One', 'GM-2', 'B-1'),
        risk_level: 'critical',
        news2: {
          score: 0.45, level: 'critical', raw_news2: 9, raw_mews: 9, single_param_3: false,
          drivers: [], scoring_model: 'NEWS2', spo2_scale: 1, complete: true,
          missing_parameters: [], coverage_note: null,
          breakdown: [
            { parameter: 'Respiratory rate', value: 37, points: 3, note: null, severe: true },
            { parameter: 'SpO2', value: 84, points: 3, note: null, severe: true },
          ],
        },
      },
    ]
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    renderPanel()
    await waitFor(() => expect(screen.getByText('Ward Two Bed One')).toBeTruthy())
    expect(names()).toEqual(['Ward One Bed One', 'Ward Two Bed One'])
    expect(document.querySelector('.patient-row__readings')).toBeNull()
    expect(document.querySelector('.patient-row__risk--critical')).toBeNull()
  })

  it('with scoring on, an unscored row still shows the not recorded dash', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('Ward One Bed Two')).toBeTruthy())
    const badges = document.querySelectorAll('.patient-row__score [role="img"]')
    expect(badges.length).toBe(4)
  })
})
