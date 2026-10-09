/**
 * The admin occupancy tile says where its bed count came from (adt-14, ruling
 * ADT-P5): the server's words from /command-center/ census.capacity_source,
 * sent only while Beds and admissions is on. With it off the tile prints
 * nothing new.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { AdministrationRecord } from '../../api/client'

/** Every apiFetch path the board touches, in order. */
const apiCalls: string[] = []

/** What /command-center/ answers with. Reassigned per case. */
let censusResponse: unknown = {
  census: { total_admitted: 30, total_capacity: 90, occupancy_pct: 33.3 },
}

const apiFetch = vi.fn(async (path: string) => {
  apiCalls.push(path)
  if (path.startsWith('/command-center')) {
    if (censusResponse instanceof Error) throw censusResponse
    return censusResponse
  }
  return {}
})

vi.mock('../../api/client', () => ({
  apiFetch: (p: string) => apiFetch(p),
  getPatientDetail: vi.fn(async () => null),
  getAdministrations: vi.fn(async () => []),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: AdministrationRecord | null | undefined) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  dischargePatient: vi.fn(), getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  addClinicalNote: vi.fn(), addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getDischargeQrObjectUrl: vi.fn(), prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  getUser: () => ({ id: 'ADM001', name: 'Rajesh Kumar', role: 'admin', department: null }),
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

/** One row on the board. The tile's numerator and denominator both come from
 *  the server census, not from the board. */
const BOARD = [{
  id: 'P0012', name: 'Anita Rao', mrn: 'MRN-12', bed: 'GM-101', ward: 'GM-1',
  admission_date: '2026-08-05', diagnosis: 'Sepsis', age: 61, sex: 'F',
  risk_score: 0.2, risk_level: 'stable', risk_drivers: [],
}]

function renderPanel(role: string | null) {
  return render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role={role} />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
}

beforeEach(() => {
  apiCalls.length = 0
  apiFetch.mockClear()
  censusResponse = {
    census: { total_admitted: 30, total_capacity: 90, occupancy_pct: 33.3 },
  }
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => BOARD,
    // The board reports 30 matches server-side; the OLD code divided this by a
    // literal 35 and printed 86%.
    headers: { get: () => '30' },
  })))
})

describe('the occupancy tile says where its bed count came from', () => {
  it('prints the server words while Beds and admissions is on', async () => {
    censusResponse = {
      census: { total_admitted: 30, total_capacity: 90, occupancy_pct: 33.3,
        capacity_source: { source: 'mixed', words: 'from bed register for 2 of 6 wards, site setting for the rest' } },
    }
    renderPanel('admin')
    await screen.findByText('Anita Rao')
    await waitFor(() => expect(screen.getByText('33.3%')).toBeTruthy())
    expect(screen.getByText('Bed Occupancy · 30/90')).toBeTruthy()
    expect(screen.getByText('Beds: from bed register for 2 of 6 wards, site setting for the rest')).toBeTruthy()
  })

  it('prints nothing new while Beds and admissions is off', async () => {
    renderPanel('admin')
    await screen.findByText('Anita Rao')
    await waitFor(() => expect(screen.getByText('33.3%')).toBeTruthy())
    expect(screen.queryByText(/^Beds: /)).toBeNull()
  })
})
