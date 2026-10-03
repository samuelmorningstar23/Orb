/**
 * ADMIN OVERVIEW — "UNKNOWN" MUST NOT RENDER AS "0".
 *
 * The server side of this was fixed on 2026-08-14: the Alert Activity and
 * Records Written tiles used to be sourced from tables only the HTTP ingest lane
 * writes, so on a box running the simulator they were structurally pinned to 0
 * while the ward showed 43 active / 17 critical. The API now sends `total: null`
 * whenever the number genuinely cannot be known.
 *
 * That fix is only half of it. `total ?? 0` in this component would put the same
 * lie back on the screen with none of the server code to blame, so the rendering
 * contract is asserted here:
 *
 *   * an unknown total renders the WORD "Unknown" — never a digit;
 *   * a genuine zero still renders "0", and the two are distinguishable;
 *   * a known total renders the number, broken down by the lane that produced it.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const getAdminOverview = vi.fn()

vi.mock('../../../api/client', () => ({
  getAdminOverview: (...args: unknown[]) => getAdminOverview(...args),
}))

import AdminOverview from './AdminOverview'

const BASE = {
  ingestion_mode: 'simulation',
  audit_simulation_enabled: false,
  audit_total_24h: 12,
  audit_categories: {},
  audit_outcomes: {},
  ingestion: { total: 0, parsed: 0, failed: 0, duplicates: 0, total_vitals: 0, total_observations: 0 },
  source_mix: {},
  recent_failures: [],
  auth_events: [],
  alerts: {
    total: 43, known: true, unknown_reason: null,
    by_lane: { simulator: 40, ingestion: 3 },
    by_level: { critical: 17, elevated: 26 },
    source: 'shadow_events',
    audit_trail: { vigil: 0, relay: 0, lane: 'ingestion', note: '' },
  },
  records_written: {
    total: 8412, counted: 8412, vitals_counted: 8000, observations_counted: 412,
    by_source: { simulated: 8000, ingested_hl7: 412 },
    complete: true, incomplete_reason: null, source: 'clinical_tables',
  },
}

const card = (label: RegExp) => screen.getByText(label).closest('.admin-stat-card') as HTMLElement

beforeEach(() => {
  getAdminOverview.mockReset()
})

describe('admin overview headline tiles', () => {
  it('shows the real count and names the lane that produced it', async () => {
    getAdminOverview.mockResolvedValue(BASE)
    render(<AdminOverview />)

    await waitFor(() => expect(screen.getByText(/Alert Activity/)).toBeInTheDocument())
    const alerts = card(/Alert Activity/)
    expect(within(alerts).getByText('43')).toBeInTheDocument()
    // The simulator lane is the one the pre-fix source could not see at all.
    // Lane names are printed in words, not as the raw key (F154).
    expect(within(alerts).getByText('Simulator: 40')).toBeInTheDocument()
    expect(within(alerts).getByText('Ingestion: 3')).toBeInTheDocument()
    expect(within(alerts).getByText('17 critical')).toBeInTheDocument()

    const records = card(/Records Written/)
    expect(within(records).getByText('8,412')).toBeInTheDocument()
  })

  it('renders an unknown alert total as "Unknown", not as 0', async () => {
    getAdminOverview.mockResolvedValue({
      ...BASE,
      alerts: {
        total: null, known: false,
        unknown_reason: 'The shadow event log is switched off (ORB_SHADOW_ENABLED).',
        by_lane: {}, by_level: {}, source: 'shadow_events',
        audit_trail: { vigil: 0, relay: 0, lane: 'ingestion', note: '' },
      },
    })
    render(<AdminOverview />)

    await waitFor(() => expect(screen.getByText(/Alert Activity/)).toBeInTheDocument())
    const alerts = card(/Alert Activity/)
    expect(within(alerts).getByText('Unknown')).toBeInTheDocument()
    expect(within(alerts).queryByText('0')).toBeNull()
    // The reviewer is told WHY, not just shown a shrug.
    expect(within(alerts).getByText(/ORB_SHADOW_ENABLED/)).toBeInTheDocument()
  })

  it('renders an unknown record total as "Unknown" and shows the floor as a floor', async () => {
    getAdminOverview.mockResolvedValue({
      ...BASE,
      records_written: {
        total: null, counted: 6000, vitals_counted: 6000, observations_counted: 0,
        by_source: { simulated: 6000 }, complete: false,
        incomplete_reason: '3 simulated patient(s) are at the 2000-row retention cap.',
        source: 'clinical_tables',
      },
    })
    render(<AdminOverview />)

    await waitFor(() => expect(screen.getByText(/Records Written/)).toBeInTheDocument())
    const records = card(/Records Written/)
    expect(within(records).getByText('Unknown')).toBeInTheDocument()
    // A floor is shown, but labelled as one — it must never occupy the headline.
    expect(within(records).getByText(/at least 6,000 recorded/)).toBeInTheDocument()
    expect(within(records).queryByText('6,000')).toBeNull()
    expect(within(records).getByText(/retention cap/)).toBeInTheDocument()
  })

  it('a genuine zero still renders as 0 and is distinguishable from unknown', async () => {
    getAdminOverview.mockResolvedValue({
      ...BASE,
      alerts: {
        total: 0, known: true, unknown_reason: null,
        by_lane: {}, by_level: { critical: 0, elevated: 0 }, source: 'shadow_events',
        audit_trail: { vigil: 0, relay: 0, lane: 'ingestion', note: '' },
      },
    })
    render(<AdminOverview />)

    await waitFor(() => expect(screen.getByText(/Alert Activity/)).toBeInTheDocument())
    const alerts = card(/Alert Activity/)
    expect(within(alerts).getByText('0')).toBeInTheDocument()
    expect(within(alerts).queryByText('Unknown')).toBeNull()
  })

  it('does not use opacity to de-emphasise the unknown value', async () => {
    getAdminOverview.mockResolvedValue({
      ...BASE,
      alerts: {
        total: null, known: false, unknown_reason: 'instrument off',
        by_lane: {}, by_level: {}, source: 'shadow_events',
        audit_trail: { vigil: 0, relay: 0, lane: 'ingestion', note: '' },
      },
    })
    render(<AdminOverview />)

    await waitFor(() => expect(screen.getByText(/Alert Activity/)).toBeInTheDocument())
    const value = within(card(/Alert Activity/)).getByText('Unknown')
    expect(value.style.opacity).toBe('')
    expect(value.style.color).toContain('--text-muted')
  })
})
