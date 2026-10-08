/**
 * FrontDeskPanel with the registration feature (opd-15, opd-3).
 *
 *  1. FEATURE OFF, NOTHING CHANGED: the two built-in sections and nothing else,
 *     and the same four requests on load.
 *  2. FEATURE ON: the sections found in ./frontdesk/*View.tsx appear as tabs,
 *     each only for a role holding its permission.
 *  3. A DESK-ONLY ACCOUNT (registration.* and no chart): no Transfers, no
 *     request for the clinical board or the transfer list, no Call in or
 *     Complete; the queue, the roster and No-show stay.
 *  4. A view that meets 'feature_disabled' drops the desk back to its two
 *     sections without a reload.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetFeatureFlags, setFeatureFlags } from '../../shellConfig'
import FrontDeskPanel from './FrontDeskPanel'
import { discoverFrontDeskViews, type FrontDeskViewProps } from './frontdesk/viewRegistry'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
}))

const DEPT = 'Cardiology'

const QUEUE = {
  visit_date: '2026-10-05',
  department: DEPT,
  queues: [{
    department: DEPT, doctor_id: 'DOC-1', doctor_name: 'Dr Iyer',
    visits: [
      { id: 'OPD-1', patient_id: 'PAT-1', patient_name: 'Anjali Rao', patient_mrn: 'MRN-1',
        patient_age: 54, patient_sex: 'F', department: DEPT, doctor_id: 'DOC-1',
        doctor_name: 'Dr Iyer', visit_date: '2026-10-05', token_number: 1, visit_type: 'new',
        chief_complaint: 'cough', status: 'waiting', registered_at: '2026-10-05T03:00:00+00:00',
        called_at: null, completed_at: null, cancelled_at: null, cancel_reason: null,
        consultation_charge_id: null, notes: null, waiting_minutes: 5,
        consultation_minutes: null },
    ],
    waiting_count: 1, in_consultation_count: 0, longest_wait_minutes: 5,
  }],
  total: 1,
}

function routeApi(extra: Record<string, () => unknown> = {}) {
  apiFetch.mockImplementation(async (path: string) => {
    for (const [prefix, fn] of Object.entries(extra)) {
      if (path.startsWith(prefix)) return fn()
    }
    if (path.startsWith('/frontdesk/opd/queue')) return QUEUE
    if (path.startsWith('/frontdesk/opd/doctors')) {
      return { doctors: [{ id: 'DOC-1', name: 'Dr Iyer', department: DEPT }] }
    }
    if (path.startsWith('/vigil/patients')) return []
    if (path.startsWith('/frontdesk/transfers/destinations')) return { destinations: [] }
    throw new Error(`unrouted ${path}`)
  })
}

function StubView({ onFeatureOff }: FrontDeskViewProps) {
  return (
    <div>
      <p>Stub view body</p>
      <button type="button" onClick={onFeatureOff}>Pretend the feature went off</button>
    </div>
  )
}

const STUB_VIEWS = discoverFrontDeskViews({
  './frontdesk/StubView.tsx': { default: StubView, viewLabel: 'Stub timings', viewOrder: 30 },
  './frontdesk/ManagersOnlyView.tsx': {
    default: StubView, viewLabel: 'Managers only', viewPermission: 'schedule.manage' },
})

const clinicianHolds = (p: string) => ['patient.read_all', 'patient.write'].includes(p)
const receptionistHolds = (p: string) => ['registration.read', 'registration.write'].includes(p)

function tabNames() {
  return within(screen.getByRole('tablist')).getAllByRole('tab').map(t => t.textContent)
}

function requested(prefix: string) {
  return apiFetch.mock.calls.some(([p]) => String(p).startsWith(prefix))
}

beforeEach(() => {
  apiFetch.mockReset()
  resetFeatureFlags()
})
afterEach(() => {
  cleanup()  // unmount first, so resetting the flags notifies nothing that is mounted
  resetFeatureFlags()
  vi.restoreAllMocks()
})

describe('FrontDeskPanel with the registration feature off', () => {
  it('shows only the OPD queue and Transfers, whatever views are installed', async () => {
    routeApi()
    render(<FrontDeskPanel views={STUB_VIEWS} holds={() => true} />)
    await screen.findByText('Anjali Rao')
    expect(tabNames()).toEqual(['OPD queue', 'Transfers'])
    expect(requested('/vigil/patients')).toBe(true)
    expect(requested('/frontdesk/transfers/destinations')).toBe(true)
  })

  it('treats an account with no chart exactly as before (no desk-only mode)', async () => {
    routeApi()
    render(<FrontDeskPanel views={STUB_VIEWS} holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    expect(tabNames()).toEqual(['OPD queue', 'Transfers'])
    expect(screen.getByRole('button', { name: 'Call in' })).toBeInTheDocument()
  })
})

describe('FrontDeskPanel with the registration feature on', () => {
  beforeEach(() => setFeatureFlags({ registration: true }))

  it('adds the installed views a role may see, after the built-in sections', async () => {
    routeApi()
    render(<FrontDeskPanel views={STUB_VIEWS} holds={clinicianHolds} />)
    await screen.findByText('Anjali Rao')
    expect(tabNames()).toEqual(['OPD queue', 'Transfers', 'Stub timings'])
    fireEvent.click(screen.getByRole('tab', { name: 'Stub timings' }))
    expect(screen.getByText('Stub view body')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Stub timings' })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows a permission-gated view only to a role that holds it', async () => {
    routeApi()
    render(<FrontDeskPanel views={STUB_VIEWS}
      holds={p => clinicianHolds(p) || p === 'schedule.manage'} />)
    await screen.findByText('Anjali Rao')
    expect(tabNames()).toContain('Managers only')
  })

  it('gives a desk-only account the queue and the roster and nothing on the chart', async () => {
    routeApi()
    render(<FrontDeskPanel views={STUB_VIEWS} holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    expect(tabNames()).toEqual(['OPD queue', 'Stub timings'])
    expect(requested('/vigil/patients')).toBe(false)
    expect(requested('/frontdesk/transfers/destinations')).toBe(false)
    expect(requested('/frontdesk/opd/doctors')).toBe(true)
    expect(screen.queryByRole('button', { name: 'Call in' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
    expect(screen.getByRole('button', { name: 'No-show' })).toBeInTheDocument()
    /* opd-6a: no ward-list visit form with the feature on; with no Register
       view installed (these stub views) the section says who registers. */
    expect(screen.queryByLabelText('Patient')).toBeNull()
    expect(screen.getByText(/Your account cannot open the index/)).toBeInTheDocument()
  })

  it('installs the real clinic timings view for a role that reads the desk', async () => {
    routeApi()
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    expect(tabNames()).toContain('Clinic timings')
  })

  it('drops back to the built-in sections when a view meets the switch going off', async () => {
    routeApi()
    render(<FrontDeskPanel views={STUB_VIEWS} holds={clinicianHolds} />)
    await screen.findByText('Anjali Rao')
    fireEvent.click(screen.getByRole('tab', { name: 'Stub timings' }))
    fireEvent.click(screen.getByRole('button', { name: 'Pretend the feature went off' }))
    await waitFor(() => expect(tabNames()).toEqual(['OPD queue', 'Transfers']))
    expect(screen.getByRole('tab', { name: 'OPD queue' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('Anjali Rao')).toBeInTheDocument()
  })
})
