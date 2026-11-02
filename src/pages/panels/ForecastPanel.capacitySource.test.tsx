/**
 * Every capacity figure says where its bed count came from (adt-14, ruling
 * ADT-P5): 'from bed register' for a ward whose register is complete, 'site
 * setting' for the rest. The server sends the words only while Beds and
 * admissions is on; with it off the screen prints nothing new.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ForecastPanel from './ForecastPanel'
import { localDateKey } from '../../lib/datetime'

vi.mock('../../api/client', async importOriginal => ({ ...(await importOriginal<typeof import('../../api/client')>()), buildHeaders: () => ({}) }))
vi.mock('../../components/ProvenanceChip', () => ({ default: () => null }))

const MIXED = { source: 'mixed', words: 'from bed register for 1 of 2 wards, site setting for the rest' }

function stubForecastApi(withSource: boolean) {
  const today = localDateKey()
  const hospital = {
    current_occupancy: 6,
    total_capacity: 16,
    current_occupancy_pct: 37.5,
    daily_forecasts: [
      { date: today, day_label: 'Today', is_current: true, actual_admissions: 0, actual_discharges: 0,
        predicted_admissions: 0, predicted_discharges: 0, predicted_occupancy: 6, occupancy_pct: 37.5,
        ward_breakdown: {} },
      { date: '2099-01-02', day_label: 'Fri 2 Jan', predicted_admissions: 1, predicted_discharges: 1,
        predicted_occupancy: 6, occupancy_pct: 37.5, ward_breakdown: {} },
    ],
    summary: {
      avg_occupancy: 6, peak_day: 'Fri 2 Jan', peak_occupancy: 6,
      total_predicted_discharges_7d: 1, total_predicted_admissions_7d: 1, beds_available_in_48h: 10,
    },
    ...(withSource ? { capacity_source: MIXED } : {}),
  }
  const capacity = {
    current: {
      'GM-1': { total: 4, occupied: 3, available: 1, utilization_pct: 75,
        ...(withSource ? { capacity_source: { source: 'bed_register', words: 'from bed register' } } : {}) },
      'CCU': { total: 12, occupied: 3, available: 9, utilization_pct: 25,
        ...(withSource ? { capacity_source: { source: 'site_setting', words: 'site setting' } } : {}) },
    },
    projected_48h: {
      'GM-1': { projected_occupied: 3, projected_available: 1, projected_utilization_pct: 75 },
      'CCU': { projected_occupied: 3, projected_available: 9, projected_utilization_pct: 25 },
    },
    total: { capacity: 16, occupied: 6, available: 10 },
    ...(withSource ? { capacity_source: MIXED } : {}),
  }
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body =
      url.includes('/api/forecast/hospital') ? hospital
      : url.includes('/api/forecast/discharge-board') ? []
      : url.includes('/api/forecast/capacity') ? capacity
      : { daily_summary: [], total_30d: 0, avg_daily: 0, avg_los: 0, severity_distribution: {}, ward_distribution: {}, discharge_rate: 0 }
    return { ok: true, json: async () => body }
  }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

async function openCapacity() {
  render(<ForecastPanel />)
  await screen.findByText('7-Day Occupancy Projection')
  await act(async () => { fireEvent.click(screen.getByRole('tab', { name: 'Ward Capacity' })) })
  await screen.findByText('GM-1')
}

describe('Forecast capacity says where each bed count came from', () => {
  it('labels each ward, the total and the census card while Beds and admissions is on', async () => {
    stubForecastApi(true)
    await openCapacity()
    expect(screen.getByText('4 beds, from bed register')).toBeInTheDocument()
    expect(screen.getByText('12 beds, site setting')).toBeInTheDocument()
    expect(screen.getByText(`Beds: ${MIXED.words}`)).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('tab', { name: 'Census Forecast' })) })
    expect(screen.getByText(`38% occupied, beds ${MIXED.words}`)).toBeInTheDocument()
  })

  it('prints nothing new while Beds and admissions is off', async () => {
    stubForecastApi(false)
    await openCapacity()
    const card = screen.getByText('GM-1').closest('.glass-card') as HTMLElement
    expect(within(card).queryByText(/from bed register|site setting/)).toBeNull()
    expect(screen.queryByText(/^Beds: /)).toBeNull()
    await act(async () => { fireEvent.click(screen.getByRole('tab', { name: 'Census Forecast' })) })
    expect(screen.getByText('38% occupied')).toBeInTheDocument()
  })
})
