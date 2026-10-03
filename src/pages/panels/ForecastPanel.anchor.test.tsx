/**
 * DEMO_READINESS §13.5 — the forecast display must anchor at today's census.
 *
 * /api/forecast/hospital used to start its daily series at TOMORROW, so the
 * chart's first point (e.g. 23) silently contradicted the Current Census card
 * and Command Center (30/32 · 94%). The invariant guarded here: the census
 * chart and daily table lead with a day-0 point equal to `current_occupancy`,
 * and a locally synthesized anchor claims no admission/discharge predictions —
 * unless the server itself sends a day 0, in which case nothing is prepended
 * and the server's MEASURED numbers are shown as actuals.
 *
 * The server now sends one. It is recognised by its `is_current` flag and NOT
 * by its date: that key is the hospital's calendar day, this browser's is its
 * own, and in IST they disagree for the first 5.5 hours of every day. Keying on
 * the date would stack a second "today" on top of the server's, every night
 * shift.
 */
import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ForecastPanel from './ForecastPanel'
import { localDateKey } from '../../lib/datetime'

vi.mock('../../api/client', () => ({
  buildHeaders: () => ({}),
}))
// The chip fetches the model registry on mount; provenance is not under test.
vi.mock('../../components/ProvenanceChip', () => ({ default: () => null }))

const dayAfter = (key: string, days: number) => {
  const d = new Date(`${key}T12:00:00`)
  d.setDate(d.getDate() + days)
  return localDateKey(d)
}

const forecastDay = (date: string, occupancy: number) => ({
  date,
  day_label: 'Tue Aug 11',
  predicted_admissions: 4,
  predicted_discharges: 6,
  predicted_occupancy: occupancy,
  occupancy_pct: Math.round((occupancy / 32) * 100),
  ward_breakdown: {},
})

/** The server's measured day 0: today's census plus what has actually happened
 *  so far today, stamped with the HOSPITAL's calendar day. */
const serverDayZero = (date: string) => ({
  date,
  day_label: 'Mon Mar 15',
  is_current: true,
  actual_admissions: 3,
  actual_discharges: 2,
  measured_occupancy: 30,
  predicted_admissions: 3,
  predicted_discharges: 2,
  predicted_occupancy: 30,
  occupancy_pct: 94,
  ward_breakdown: {},
})

function hospitalPayload(firstForecastDate: string, lead: unknown[] = []) {
  return {
    current_occupancy: 30,
    total_capacity: 32,
    current_occupancy_pct: 93.75,
    daily_forecasts: [
      ...lead,
      forecastDay(firstForecastDate, 23),
      forecastDay(dayAfter(firstForecastDate, 1), 25),
    ],
    summary: {
      avg_occupancy: 24,
      peak_day: 'Tue Aug 11',
      peak_occupancy: 25,
      total_predicted_discharges_7d: 12,
      total_predicted_admissions_7d: 8,
      beds_available_in_48h: 5,
    },
  }
}

function stubForecastApi(hospital: unknown) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body =
      url.includes('/api/forecast/hospital') ? hospital
      : url.includes('/api/forecast/discharge-board') ? []
      : url.includes('/api/forecast/capacity') ? { current: {}, projected_48h: {}, total: { capacity: 32, occupied: 30, available: 2 } }
      : { daily_summary: [], total_30d: 0, avg_daily: 0, avg_los: 0, severity_distribution: {}, ward_distribution: {}, discharge_rate: 0 }
    return { ok: true, json: async () => body }
  }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ForecastPanel day-0 anchor (§13.5)', () => {
  it('leads the daily table with the current census, not tomorrow', async () => {
    stubForecastApi(hospitalPayload(dayAfter(localDateKey(), 1)))
    render(<ForecastPanel />)

    const anchorCell = await screen.findByText('Today (current census)')
    const anchorRow = anchorCell.closest('tr')!
    // Census column carries the measured 30; the prediction columns are
    // explicit em dashes — the anchor must not claim +4/-6 flow it was
    // never predicted.
    expect(within(anchorRow).getByText('30')).toBeInTheDocument()
    expect(within(anchorRow).getAllByText('—')).toHaveLength(2)
    expect(within(anchorRow).queryByText('+4')).not.toBeInTheDocument()

    // The anchor row is the FIRST row, ahead of the server's tomorrow (23).
    const rows = anchorRow.closest('tbody')!.querySelectorAll('tr')
    expect(rows[0]).toBe(anchorRow)
    expect(within(rows[1]).getByText('23')).toBeInTheDocument()

    // The chart's x-axis now starts at a "Today" tick.
    expect(screen.getAllByText('Today').length).toBeGreaterThan(0)
  })

  it('does not double-anchor if the server already sends today', async () => {
    stubForecastApi(hospitalPayload(localDateKey()))
    render(<ForecastPanel />)

    await screen.findByText('7-Day Occupancy Projection')
    expect(screen.queryByText('Today (current census)')).not.toBeInTheDocument()
  })

  it('steps aside for the server day 0 even when its date key is not this browser\'s', async () => {
    // The appliance's calendar day, which a viewer in another zone — or any
    // viewer at all, back when the server keyed off UTC — does not share.
    const hospitalDay = dayAfter(localDateKey(), -1)
    stubForecastApi(hospitalPayload(dayAfter(localDateKey(), 1),
                                    [serverDayZero(hospitalDay)]))
    render(<ForecastPanel />)

    const dayZeroCell = await screen.findByText('Today (actual so far)')
    const rows = dayZeroCell.closest('tbody')!.querySelectorAll('tr')
    // Three rows, not four: no locally synthesized anchor on top of the
    // server's own day 0.
    expect(rows).toHaveLength(3)
    expect(rows[0]).toBe(dayZeroCell.closest('tr'))
    expect(screen.queryByText('Today (current census)')).not.toBeInTheDocument()

    // Measured numbers are shown as what they are, not hidden behind em dashes
    // and not dressed up as predictions.
    const dayZeroRow = dayZeroCell.closest('tr')!
    expect(within(dayZeroRow).getByText('+3')).toBeInTheDocument()
    expect(within(dayZeroRow).getByText('-2')).toBeInTheDocument()
    expect(within(dayZeroRow).getByText('30')).toBeInTheDocument()
    expect(within(dayZeroRow).getByTitle('Admissions recorded so far today')).toBeInTheDocument()
  })
})
