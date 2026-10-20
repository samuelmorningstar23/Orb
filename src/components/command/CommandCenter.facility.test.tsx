/**
 * The Command Center with more than one facility of a hospital group
 * (multisite-12).
 *
 * The server sends `facility_view`, the facilities the reader may choose and,
 * for a group-wide reader, one tile per facility. The screen states which
 * facility the figures are about, offers a selector only to a reader who may
 * see more than one facility, and asks the server for the facility chosen.
 * With one facility on record the screen reads as it always has.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import CommandCenter from './CommandCenter'

const NORTH = { id: 1, code: 'NTH', name: 'Northgate Hospital' }
const RIVER = { id: 2, code: 'RVS', name: 'Riverside Hospital' }

const SNAPSHOT = {
  generated_at: '2026-10-20T09:00:00+00:00',
  census: { total_admitted: 9, total_capacity: 13, occupancy_pct: 69, by_ward: [] },
  acuity: { critical: 2, elevated: 1, stable: 6, heatmap: [] },
  deteriorations: [],
  bundles: { active: 0, compliant: 0, compliance_pct: 100 },
  forecast: { projected_admissions_24h: 1, projected_occupancy: 10 },
  downtime_readiness: { patients_with_recent_snapshot: 9, total: 9, pct: 100, oldest_snapshot_age_min: 5 },
  or_today: { count: 0, cases: [] },
}

const GROUP = {
  ...SNAPSHOT,
  facility: null,
  facility_view: 'group',
  facilities: [NORTH, RIVER],
  facility_tiles: [
    { facility: NORTH, total_admitted: 5, total_capacity: 6, occupancy_pct: 83,
      critical: 1, elevated: 1, stable: 3, not_scored: 0 },
    { facility: RIVER, total_admitted: 4, total_capacity: 4, occupancy_pct: 100,
      critical: 1, elevated: 0, stable: 3, not_scored: 0 },
  ],
  unplaced_capacity: 3,
}

beforeEach(() => {
  apiFetch.mockReset()
})

describe('a group-wide reader', () => {
  it('sees the group, one tile per facility and the beds placed at none', async () => {
    apiFetch.mockResolvedValue(GROUP)
    render(<CommandCenter />)

    expect(await screen.findByText('Every facility of the group on one screen')).toBeTruthy()
    expect(apiFetch.mock.calls[0][0]).toBe('/command-center/')
    const tiles = screen.getByRole('region', { name: 'Facilities of the group' })
    const north = within(tiles).getByText('Northgate Hospital (NTH)').closest('.cmd-kpi') as HTMLElement
    expect(north.querySelector('.cmd-kpi__value')?.textContent).toBe('5/6')
    const river = within(tiles).getByText('Riverside Hospital (RVS)').closest('.cmd-kpi') as HTMLElement
    expect(river.querySelector('.cmd-kpi__value')?.textContent).toBe('4/4')
    const unplaced = within(tiles).getByText('Beds at no facility').closest('.cmd-kpi') as HTMLElement
    expect(unplaced.querySelector('.cmd-kpi__value')?.textContent).toBe('3')
  })

  it('narrows to the facility chosen', async () => {
    apiFetch.mockResolvedValueOnce(GROUP)
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      census: { total_admitted: 4, total_capacity: 4, occupancy_pct: 100, by_ward: [] },
      facility: RIVER,
      facility_view: 'facility',
      facilities: [NORTH, RIVER],
    })
    render(<CommandCenter />)

    const select = await screen.findByRole('combobox', { name: 'Facility' }) as HTMLSelectElement
    expect([...select.options].map(o => o.textContent)).toEqual([
      'All facilities', 'Northgate Hospital (NTH)', 'Riverside Hospital (RVS)',
    ])
    fireEvent.change(select, { target: { value: '2' } })

    await waitFor(() => expect(apiFetch).toHaveBeenLastCalledWith('/command-center/?facility=2'))
    expect(await screen.findByText('Riverside Hospital (RVS) on one screen')).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Facilities of the group' })).toBeNull()
  })
})

describe('a reader bound to one facility', () => {
  it('is told the facility, gets no selector and no house-wide wording', async () => {
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      facility: RIVER,
      facility_view: 'facility',
      facilities: [RIVER],
      deteriorations_scope: 'Cardiology',
      deteriorations_house_count: 2,
    })
    render(<CommandCenter />)

    expect(await screen.findByText('Riverside Hospital (RVS) on one screen')).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'Facility' })).toBeNull()
    expect(screen.getByText('0 of 2 at Riverside Hospital (RVS)')).toBeTruthy()
    expect(screen.queryByText(/house-wide/)).toBeNull()
  })

  it('says so when the department is listed at no facility', async () => {
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      facility: null,
      facility_view: 'no_facility',
      facilities: [],
    })
    render(<CommandCenter />)

    expect(await screen.findByText(
      'Your department is not listed at any facility, so there is nothing to count',
    )).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'Facility' })).toBeNull()
  })
})

describe('one facility on record', () => {
  it('reads as it always has', async () => {
    apiFetch.mockResolvedValue(SNAPSHOT)
    render(<CommandCenter />)

    expect(await screen.findByText('House-wide operations on one screen')).toBeTruthy()
    expect(apiFetch.mock.calls[0][0]).toBe('/command-center/')
    expect(screen.queryByRole('combobox', { name: 'Facility' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Facilities of the group' })).toBeNull()
  })
})
