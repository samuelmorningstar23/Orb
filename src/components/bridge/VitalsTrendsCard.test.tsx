/**
 * Patient-facing vitals say WHEN they were measured (DEMO_READINESS §14.3).
 *
 * The invariant under test is HONESTY about time: the portal showed
 * "73.9 bpm — In normal range" from readings seven days old with no "as of"
 * date. A reading from another day must carry its date ("Aug 9, 14:32"), a
 * same-day reading reads "Today 14:32", and a reading with no timestamp shows
 * nothing — never a fabricated "now".
 *
 * NO VERDICT (ruled 2026-10-03). The card also judged each vital "In normal
 * range" / "Below normal" / "Above normal" against portal-only bands, coloured
 * in status FILL tokens. SBP 90 (NEWS2 3 points, red on the ward) read normal,
 * and HR 100, SBP 91, RR 20, Temp 37.0 and SpO2 94 on oxygen (NEWS2 6) read
 * normal on every row. The card now shows the number as charted, when it was
 * measured and a neutral trend, and says the care team reviews the readings.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import type { VitalTrend } from '../../api/bridgeClient'

const mockGetVitalsTrends = vi.fn()
vi.mock('../../api/bridgeClient', () => ({
  getVitalsTrends: (...args: unknown[]) => mockGetVitalsTrends(...args),
}))

import { VitalsTrendsCard } from './VitalsTrendsCard'

const CARE_TEAM_LINE =
  'Your care team reviews every reading in the context of your condition. ' +
  'If you feel worse, tell your nurse straight away.'

function trend(overrides: Partial<VitalTrend>): VitalTrend {
  return {
    label: 'Heart rate',
    unit: 'bpm',
    current: 74,
    measured_at: null,
    series: [72, 74, 74],
    ...overrides,
  }
}

function mockTrends(trends: Record<string, VitalTrend>) {
  mockGetVitalsTrends.mockResolvedValueOnce({ trends, count: 1 })
}

describe('VitalsTrendsCard measurement timestamps', () => {
  it('shows the date and time for a non-today reading', async () => {
    mockTrends({ heart_rate: trend({ measured_at: '2026-08-09T14:32:00' }) })
    render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('74')).toBeInTheDocument())
    expect(screen.getByText(/Aug 9, 14:32/)).toBeInTheDocument()
    expect(screen.queryByText(/Today/)).toBeNull()
  })

  it('shows "Today HH:MM" for a same-day reading', async () => {
    const now = new Date()
    const iso = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}T09:05:00`
    mockTrends({ heart_rate: trend({ measured_at: iso }) })
    render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('74')).toBeInTheDocument())
    expect(screen.getByText(/Today 09:05/)).toBeInTheDocument()
  })

  it('shows no timestamp at all when measured_at is absent — never "now"', async () => {
    mockTrends({ heart_rate: trend({ measured_at: null }) })
    const { container } = render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('74')).toBeInTheDocument())
    expect(container.textContent).not.toMatch(/\d{2}:\d{2}/)
    expect(container.textContent).not.toContain('Today')
  })

  it('treats an unparseable timestamp as absent', async () => {
    mockTrends({ heart_rate: trend({ measured_at: 'not-a-date' }) })
    const { container } = render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('74')).toBeInTheDocument())
    expect(container.textContent).not.toMatch(/\d{2}:\d{2}/)
  })
})

describe('VitalsTrendsCard gives no verdict on a vital', () => {
  // The verdict fields the server sent before 2026-10-03. A card served beside
  // an older server must still say nothing with them.
  const legacy = { status: 'normal', normal_low: 90, normal_high: 140 }
  const sbp90 = {
    systolic_bp: {
      ...trend({ label: 'Systolic BP', unit: 'mmHg', current: 90, series: [96, 90] }),
      ...legacy,
    } as VitalTrend,
  }

  it('never calls a reading normal, low or high', async () => {
    mockTrends(sbp90)
    const { container } = render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('90')).toBeInTheDocument())
    expect(container.textContent).not.toMatch(/\b(normal|below|above|high|low)\b/i)
  })

  it('paints nothing in a status colour', async () => {
    mockTrends(sbp90)
    const { container } = render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('90')).toBeInTheDocument())
    expect(container.innerHTML).not.toContain('--status-')
  })

  it('says the care team reviews the readings', async () => {
    mockTrends(sbp90)
    render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText(CARE_TEAM_LINE)).toBeInTheDocument())
  })

  it('prints each value as charted, whatever arrives on the wire', async () => {
    mockTrends({
      heart_rate: trend({ current: 73.9 }),
      temperature: trend({ label: 'Temperature', unit: '°C', current: 37, series: [36.8, 37] }),
      spo2: trend({ label: 'Oxygen (SpO₂)', unit: '%', current: 95.5, series: [96, 95.5] }),
    })
    render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('74')).toBeInTheDocument())
    // Temperature keeps its tenth: JSON 37.0 arrives as the number 37.
    expect(screen.getByText('37.0')).toBeInTheDocument()
    expect(screen.getByText('96')).toBeInTheDocument()
    expect(screen.queryByText('73.9')).toBeNull()
  })

  it('says there is no reading when a vital was never measured', async () => {
    mockTrends({ respiratory_rate: trend({ label: 'Respiratory rate', unit: '/min', current: null, series: [] }) })
    render(<VitalsTrendsCard />)
    await waitFor(() => expect(screen.getByText('No recent reading')).toBeInTheDocument())
  })
})
