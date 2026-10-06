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
    expect(screen.getByText('No elevated or critical NEWS2 scores in Cardiology.')).toBeTruthy()
    expect(screen.queryByText(/House is stable/)).toBeNull()
  })

  it('keeps the house-wide wording for an unscoped viewer', async () => {
    apiFetch.mockResolvedValue(SNAPSHOT)
    render(<CommandCenter />)

    expect(await screen.findByText('No elevated or critical NEWS2 scores. House is stable.')).toBeTruthy()
    expect(screen.queryByText(/ only$/)).toBeNull()
  })
})

/* 2026-10-06: the critical count carries its denominator, and patients with no
   observation are counted as not scored rather than folded into stable. */
describe('command center critical count', () => {
  it('says how many of the admitted patients are critical', async () => {
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      census: { ...SNAPSHOT.census, total_admitted: 24 },
      acuity: { critical: 3, elevated: 2, stable: 17, not_scored: 2, heatmap: [] },
    })
    render(<CommandCenter />)
    const label = await screen.findByText('Critical Patients')
    const kpi = label.closest('.cmd-kpi') as HTMLElement
    expect(kpi.querySelector('.cmd-kpi__value')?.textContent).toBe('3 of 24 admitted')
    expect(kpi.querySelector('.cmd-kpi__foot')?.textContent).toBe('2 elevated · 17 stable · 2 not scored')
  })
})

/* The heatmap counted the patients with no observation nowhere, so a ward's
   parts no longer added up to its beds occupied. They now show as their own
   neutral count. An incomplete set's NEWS2 is a minimum, as on the board. */
describe('command center not scored and incomplete sets', () => {
  it('names the not scored patients on the ward cell', async () => {
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      census: { ...SNAPSHOT.census, total_admitted: 6 },
      acuity: {
        critical: 1, elevated: 1, stable: 2, not_scored: 2,
        heatmap: [{ ward: 'ICU', critical: 1, elevated: 1, stable: 2, not_scored: 2 }],
      },
    })
    render(<CommandCenter />)
    const cell = await screen.findByRole('group', { name: /^ICU:/ })
    expect(cell.getAttribute('aria-label')).toContain('2 stable, 2 not scored')
    expect(cell.querySelector('.cmd-heat__counts em')?.textContent).toBe('2 not scored')
    expect(cell.querySelector('.cmd-heat__seg--unscored')).toBeTruthy()
  })

  it('prints an incomplete set as a minimum', async () => {
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      deteriorations: [{ patient_id: 'P1', name: 'Asha Rao', ward: 'ICU', bed: '3',
                         level: 'elevated', news2: 5, news2_complete: false }],
    })
    render(<CommandCenter />)
    expect(await screen.findByText('NEWS2 ≥5')).toBeTruthy()
  })
})

/* With scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record) there
   is no band: the critical count, the acuity heatmap and the highest-scores
   list are not drawn, so nothing says "House is stable" or "not scored" about
   patients whose observations are on file. Census and the rest still show. */
describe('command center with scoring switched off', () => {
  it('draws no critical count, heatmap or highest-scores list when the server says so', async () => {
    apiFetch.mockResolvedValue({
      ...SNAPSHOT,
      scoring_enabled: false,
      acuity: { critical: 0, elevated: 0, stable: 0, not_scored: 30, heatmap: [] },
    })
    render(<CommandCenter />)
    expect(await screen.findByText('Census / Occupancy')).toBeTruthy()
    expect(screen.queryByText('Critical Patients')).toBeNull()
    expect(screen.queryByText('Ward Acuity Heatmap')).toBeNull()
    expect(screen.queryByText('Highest NEWS2 scores')).toBeNull()
    expect(screen.queryByText(/House is stable/)).toBeNull()
  })

  it('does the same when the shell switch is off', async () => {
    const { act } = await import('@testing-library/react')
    const { setClinicalSwitches, resetClinicalSwitches } = await import('../../shellConfig')
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    try {
      apiFetch.mockResolvedValue(SNAPSHOT)
      render(<CommandCenter />)
      expect(await screen.findByText('Census / Occupancy')).toBeTruthy()
      expect(screen.queryByText('Critical Patients')).toBeNull()
      expect(screen.queryByText('Highest NEWS2 scores')).toBeNull()
    } finally {
      act(() => { resetClinicalSwitches() })
    }
  })
})
