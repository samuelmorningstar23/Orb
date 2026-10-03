/**
 * Forecast says what it is, and prints counts a planner can read.
 *
 * The hero claimed "a multi-task LSTM neural network" beside its own
 * "untrained" provenance chip and a footer saying the figures are rule-based.
 * A day with no discharges printed "-0". The section strip was four bare
 * buttons with a glow, squeezed onto one line on a phone.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ForecastPanel from './ForecastPanel'
import { localDateKey } from '../../lib/datetime'

vi.mock('../../api/client', () => ({ buildHeaders: () => ({}) }))
vi.mock('../../components/ProvenanceChip', () => ({ default: () => null }))

const day = (date: string, admissions: number, discharges: number, occupancy: number) => ({
  date,
  day_label: 'Wed Aug 12',
  predicted_admissions: admissions,
  predicted_discharges: discharges,
  predicted_occupancy: occupancy,
  occupancy_pct: Math.round((occupancy / 32) * 100),
  ward_breakdown: {},
})

function stubForecastApi() {
  const today = localDateKey()
  const hospital = {
    current_occupancy: 30,
    total_capacity: 32,
    current_occupancy_pct: 93.75,
    daily_forecasts: [
      { ...day(today, 0, 0, 30), is_current: true, actual_admissions: 0, actual_discharges: 0 },
      day('2099-01-02', 4, 6, 28),
    ],
    summary: {
      avg_occupancy: 29, peak_day: 'Wed Aug 12', peak_occupancy: 30,
      total_predicted_discharges_7d: 6, total_predicted_admissions_7d: 4,
      beds_available_in_48h: 4,
    },
  }
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

describe('ForecastPanel honesty and legibility', () => {
  it('does not claim a trained neural network', async () => {
    stubForecastApi()
    const { container } = render(<ForecastPanel />)
    await screen.findByText('7-Day Occupancy Projection')
    const hero = container.querySelector('.forecast-hero-desc')!
    expect(hero.textContent).not.toMatch(/LSTM|neural/i)
    expect(hero.textContent).toContain('The forecasting model is not yet trained.')
  })

  it('prints a day with no admissions or discharges as 0, never -0 or +0', async () => {
    stubForecastApi()
    render(<ForecastPanel />)
    const row = (await screen.findByText('Today (actual so far)')).closest('tr')!
    expect(within(row).queryByText('-0')).toBeNull()
    expect(within(row).queryByText('+0')).toBeNull()
    expect(within(row).getAllByText('0')).toHaveLength(2)
    // Non-zero days keep their sign.
    const next = row.nextElementSibling as HTMLElement
    expect(within(next).getByText('+4')).toBeTruthy()
    expect(within(next).getByText('-6')).toBeTruthy()
  })

  it('the section strip is a keyboard tablist', async () => {
    stubForecastApi()
    render(<ForecastPanel />)
    await screen.findByText('7-Day Occupancy Projection')
    const list = screen.getByRole('tablist', { name: 'Forecast sections' })
    const tabs = within(list).getAllByRole('tab')
    expect(tabs.map(t => t.textContent)).toEqual([
      'Census Forecast', 'Ward Capacity', 'Discharge Board', 'Admission Patterns',
    ])
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
    expect(tabs.filter(t => t.tabIndex === 0)).toHaveLength(1)
    // No glow: state is a fill and a hairline.
    expect(tabs[0].style.boxShadow).toBe('')
    await act(async () => { fireEvent.keyDown(tabs[0], { key: 'ArrowRight' }) })
    expect(screen.getByRole('tab', { name: 'Ward Capacity' }).getAttribute('aria-selected')).toBe('true')
  })
})
