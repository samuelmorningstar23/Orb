/**
 * A WARD-SCOPED DETERIORATION LIST MUST SAY IT IS WARD-SCOPED.
 *
 * The server filters the named list to the viewer's department (a nurse holds
 * forecast.read) and sends `deteriorations_scope` so the screen can state the
 * boundary. The card never rendered it, and an empty scoped list read
 * "House is stable" while other wards had critical patients.
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import CommandCenter from './CommandCenter'

const SNAPSHOT = {
  generated_at: '2026-10-02T09:00:00+00:00',
  census: { total_admitted: 30, total_capacity: 48, occupancy_pct: 62, by_ward: [] },
  acuity: { critical: 4, elevated: 3, stable: 23, heatmap: [] },
  deteriorations: [],
  bundles: { active: 0, compliant: 0, compliance_pct: 100 },
  forecast: { projected_admissions_24h: 2, projected_occupancy: 31 },
  downtime_readiness: { patients_with_recent_snapshot: 30, total: 30, pct: 100, oldest_snapshot_age_min: 5 },
  or_today: { count: 0, cases: [] },
}

beforeEach(() => {
  apiFetch.mockReset()
})

describe('command center deteriorations card', () => {
  it('states the ward boundary and does not call the house stable', async () => {
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      deteriorations_scope: 'Cardiology',
      deteriorations_house_count: 7,
    })
    render(<CommandCenter />)

    expect(await screen.findByText('Cardiology only')).toBeTruthy()
    expect(screen.getByText('0 of 7 house-wide')).toBeTruthy()
    expect(screen.getByText('No active deteriorations in Cardiology.')).toBeTruthy()
    expect(screen.queryByText(/House is stable/)).toBeNull()
  })

  it('keeps the house-wide wording for an unscoped viewer', async () => {
    apiFetch.mockResolvedValue(SNAPSHOT)
    render(<CommandCenter />)

    expect(await screen.findByText('No active deteriorations. House is stable.')).toBeTruthy()
    expect(screen.queryByText(/ only$/)).toBeNull()
  })
})
