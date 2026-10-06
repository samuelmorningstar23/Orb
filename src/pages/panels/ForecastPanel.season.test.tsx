/**
 * Forecast shows the season as one line of context.
 *
 * Pulse's built-in seasonal calendar (GET /api/pulse/season) names what is
 * active today for the hospital's state, and /api/forecast/hospital carries the
 * same entries as a top-level `season` list. The forecast is not multiplied by
 * any of it: the weather, air quality and flu multipliers went with the live
 * feeds when Orb became strictly offline. The panel only names the season.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ForecastPanel from './ForecastPanel'
import { localDateKey } from '../../lib/datetime'

vi.mock('../../api/client', () => ({ buildHeaders: () => ({}) }))
vi.mock('../../components/ProvenanceChip', () => ({ default: () => null }))

function stubForecastApi(season?: unknown) {
  const today = localDateKey()
  const hospital = {
    current_occupancy: 20,
    total_capacity: 32,
    current_occupancy_pct: 62.5,
    daily_forecasts: [
      {
        date: today, day_label: 'Tue Oct 06', is_current: true,
        predicted_admissions: 1, predicted_discharges: 2, predicted_occupancy: 20,
        actual_admissions: 1, actual_discharges: 2, occupancy_pct: 62.5, ward_breakdown: {},
      },
      {
        date: '2099-01-02', day_label: 'Wed Oct 07',
        predicted_admissions: 4, predicted_discharges: 3, predicted_occupancy: 21,
        occupancy_pct: 65.6, ward_breakdown: {},
      },
    ],
    ...(season === undefined ? {} : { season }),
    summary: {
      avg_occupancy: 21, peak_day: 'Wed Oct 07', peak_occupancy: 21,
      total_predicted_discharges_7d: 3, total_predicted_admissions_7d: 4,
      beds_available_in_48h: 11,
    },
  }
  const fetchMock = vi.fn(async (url: string) => {
    const body =
      url.includes('/api/forecast/hospital') ? hospital
      : url.includes('/api/forecast/discharge-board') ? []
      : url.includes('/api/forecast/capacity') ? { current: {}, projected_48h: {}, total: { capacity: 32, occupied: 20, available: 12 } }
      : { daily_summary: [], total_30d: 0, avg_daily: 0, avg_los: 0, severity_distribution: {}, ward_distribution: {}, discharge_rate: 0 }
    return { ok: true, json: async () => body }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ForecastPanel season line', () => {
  it('names the active seasons, marking the ones at their peak', async () => {
    stubForecastApi([
      { id: 'sw-monsoon', title: 'South-west monsoon', kind: 'season', in_peak: true },
      { id: 'dengue', title: 'Dengue season', kind: 'disease', in_peak: false },
    ])
    render(<ForecastPanel />)
    await screen.findByText('7-Day Occupancy Projection')
    expect(screen.getByText('Season: South-west monsoon (peak), Dengue season')).toBeInTheDocument()
  })

  it('shows no season line when nothing is active', async () => {
    stubForecastApi([])
    render(<ForecastPanel />)
    await screen.findByText('7-Day Occupancy Projection')
    expect(screen.queryByText(/^Season:/)).toBeNull()
  })

  it('shows no season line when the server sends none', async () => {
    stubForecastApi()
    render(<ForecastPanel />)
    await screen.findByText('7-Day Occupancy Projection')
    expect(screen.queryByText(/^Season:/)).toBeNull()
  })

  it('asks Pulse for nothing itself: the season comes with the forecast', async () => {
    const fetchMock = stubForecastApi([
      { id: 'sw-monsoon', title: 'South-west monsoon', kind: 'season', in_peak: false },
    ])
    render(<ForecastPanel />)
    await screen.findByText('Season: South-west monsoon')
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls.some(u => u.includes('/pulse'))).toBe(false)
  })
})
