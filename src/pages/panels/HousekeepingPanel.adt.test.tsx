/**
 * Housekeeping's 'Beds & rooms' with Beds and admissions on (adt-7).
 *
 * Bed setup is then the only register of beds (the server refuses a bed from
 * this screen, 409), so the register form is not offered: the screen points
 * to Bed setup on the Beds tab, and the beds already registered stay listed.
 * With the module off the form is unchanged (HousekeepingPanel.test.tsx).
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import HousekeepingPanel from './HousekeepingPanel'
import { GOTO_TAB_EVENT } from '../../lib/orbBus'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'

vi.mock('../../api/housekeeping', async () => {
  const actual = await vi.importActual<typeof import('../../api/housekeeping')>(
    '../../api/housekeeping',
  )
  return {
    ...actual,
    getMeta: vi.fn(),
    getOverview: vi.fn(),
    getUnits: vi.fn(),
    getTasks: vi.fn(),
    getTurnaround: vi.fn(),
    getLinen: vi.fn(),
  }
})

const api = await import('../../api/housekeeping')

const META = {
  states: ['occupied', 'vacant_dirty', 'cleaning_in_progress', 'vacant_ready', 'out_of_service'],
  legal_transitions: { vacant_ready: ['occupied', 'vacant_dirty', 'out_of_service'] },
  request_types: ['routine'], priorities: ['routine'], clean_types: ['standard', 'terminal'],
  linen_directions: [], linen_items: [], expected_minutes: { standard: 45, terminal: 120 },
} as unknown as Awaited<ReturnType<typeof api.getMeta>>

const OVERVIEW = {
  counts: { open: 0, in_progress: 0, overdue: 0, terminal_open: 0, immediate: 0 },
  queue: [],
  readiness: { wards: [], capacity_basis: 'Capacity note.', reconciliation_note: '' },
  discharge_sweep: { created: 0, already_present: 0, unmatched: 0, unmatched_wards: {}, scanned: 0,
                     lookback_hours: 72 },
  expected_minutes: { standard: 45, terminal: 120 },
} as unknown as Awaited<ReturnType<typeof api.getOverview>>

const UNIT = {
  id: 'U9', unit_code: 'GM-1/01', ward: 'GM-1', room: null, bed_label: '01', unit_type: 'bed',
  is_isolation_room: false, state: 'vacant_ready', state_since: '2026-10-09T04:00:00+00:00',
  notes: null, is_active: true, minutes_in_state: 10,
}

function load() {
  vi.mocked(api.getMeta).mockResolvedValue(META)
  vi.mocked(api.getOverview).mockResolvedValue(OVERVIEW)
  vi.mocked(api.getUnits).mockResolvedValue([UNIT] as unknown as Awaited<ReturnType<typeof api.getUnits>>)
  vi.mocked(api.getTasks).mockResolvedValue([])
}

afterEach(() => {
  cleanup()
  resetModuleFlags()
  vi.clearAllMocks()
})

describe('Beds & rooms with beds and admissions on', () => {
  it('points to Bed setup instead of the register form, and keeps the beds listed', async () => {
    setModuleFlags({ adt: true })
    load()
    const opened: string[] = []
    const listen = (e: Event) => opened.push((e as CustomEvent).detail?.tab)
    window.addEventListener(GOTO_TAB_EVENT, listen)
    render(<HousekeepingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Beds & rooms' }))
    expect(await screen.findByTestId('hk-beds-in-bed-setup')).toHaveTextContent(
      'Beds are set up in Bed setup on the Beds tab')
    expect(screen.queryByRole('button', { name: 'Register bed' })).toBeNull()
    expect(screen.getAllByText('GM-1/01').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Open Beds' }))
    window.removeEventListener(GOTO_TAB_EVENT, listen)
    expect(opened).toEqual(['adt'])
  })

  it('offers the register form while the module is off', async () => {
    load()
    render(<HousekeepingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Beds & rooms' }))
    expect(await screen.findByRole('button', { name: 'Register bed' })).toBeInTheDocument()
    expect(screen.queryByTestId('hk-beds-in-bed-setup')).toBeNull()
  })
})
