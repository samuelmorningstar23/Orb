/**
 * BED OCCUPANCY ON THE WARD BOARD COMES FROM THE SHARED CAPACITY SOURCE.
 *
 * The tile used to render `admitted / 35` with the 35 written into the JSX. The
 * site's real capacity is 90 (six wards x ORB_WARD_BEDS), so 30 admitted
 * patients were announced as 86% occupancy against a true 33% — wrong by a
 * factor of two and a half, on the board a bed manager uses to decide whether
 * the hospital can take the next ambulance.
 *
 * Three properties, and the first is the one that makes the other two matter:
 *
 *   1. THE NUMBER IS THE SERVER'S. The tile copies `census.occupancy_pct` out of
 *      /command-center/ — the same snapshot the Command Center KPI and the
 *      capacity forecast are computed from — instead of dividing anything
 *      locally. A tile that did its own arithmetic could still be right today
 *      and drift tomorrow; one that copies cannot drift at all.
 *   2. UNKNOWN CAPACITY RENDERS AS UNKNOWN. A site whose bed count Orb has not
 *      been given gets an em dash, not a percentage of an invented denominator.
 *   3. IT IS FETCHED ONLY FOR THE ROLE THAT SEES IT. /command-center/ assembles
 *      the whole house snapshot; every ward tablet must not pay for a tile it is
 *      never shown.
 *
 * Proven able to fail: restore `admitted / 35` in VigilPanel.tsx and case 1 goes
 * red with the old 86%; drop the `total_capacity > 0` guard and case 2 goes red
 * with "0%" where the em dash should be.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

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
  givenAcrossAllergy: (r: any) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
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
import { ActivePatientProvider } from '../../contexts/ActivePatientContext'
import { ActiveViewProvider } from '../../contexts/ActiveViewContext'

/** One row on the board. The board's own count is irrelevant to the tile — the
 *  denominator and the numerator both come from the server census — which is
 *  exactly what makes the "1 loaded row, 33.3%" case below meaningful. */
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

describe('bed occupancy uses the shared capacity source', () => {
  it('renders the census percentage verbatim, not a local division', async () => {
    renderPanel('admin')
    await screen.findByText('Anita Rao')

    // The server's own figure, to its own precision. 30/90 = 33.3%.
    await waitFor(() => expect(screen.getByText('33.3%')).toBeTruthy())
    // ...and the denominator on screen is the site's, not a literal.
    expect(screen.getByText('Bed Occupancy · 30/90')).toBeTruthy()

    // The defect this replaces: 30 admitted over a hardcoded 35 beds.
    expect(screen.queryByText('86%')).toBeNull()

    // It came from the capacity API the Command Center and Forecast share.
    expect(apiCalls.some(p => p.startsWith('/command-center'))).toBe(true)
  })

  it('never divides by a literal — a different site capacity moves the tile', async () => {
    // Same board, a site with half the beds. If any denominator were baked into
    // the component this number could not change.
    censusResponse = {
      census: { total_admitted: 30, total_capacity: 45, occupancy_pct: 66.7 },
    }
    renderPanel('admin')
    await screen.findByText('Anita Rao')

    await waitFor(() => expect(screen.getByText('66.7%')).toBeTruthy())
    expect(screen.getByText('Bed Occupancy · 30/45')).toBeTruthy()
  })

  it('says UNKNOWN when the site has no configured bed count', async () => {
    censusResponse = { census: { total_admitted: 30, total_capacity: 0, occupancy_pct: 0 } }
    renderPanel('admin')
    await screen.findByText('Anita Rao')

    await waitFor(() =>
      expect(screen.getByText('Bed Occupancy · capacity unknown')).toBeTruthy())
    // A capacity of zero is not 0% occupancy, and it is certainly not 100%.
    expect(screen.queryByText('0%')).toBeNull()
    expect(screen.queryByText('100%')).toBeNull()
  })

  it('says UNKNOWN when the census cannot be reached at all', async () => {
    censusResponse = new Error('command center unavailable')
    renderPanel('admin')
    await screen.findByText('Anita Rao')

    await waitFor(() =>
      expect(screen.getByText('Bed Occupancy · capacity unknown')).toBeTruthy())
  })

  it('is not fetched, or shown, for a clinician role', async () => {
    renderPanel('doctor')
    await screen.findByText('Anita Rao')

    expect(screen.queryByText(/Bed Occupancy/)).toBeNull()
    expect(apiCalls.some(p => p.startsWith('/command-center'))).toBe(false)
  })
})
