/**
 * THE ALERTS TAB LISTS WHAT THE OVERVIEW COUNTS.
 *
 * It used to read the audit log's ingestion rows, which only the device-feed
 * lane writes, so a simulator box showed "No alert events" beside an Overview
 * tile counting 68. It now reads GET /admin/alerts (the tile's own source),
 * pages on the server, names each alert's lane, and says "unknown" rather than
 * "none" when the alert log is switched off.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import AlertOperations from './AlertOperations'

const ROWS = [
  {
    id: 2, episode_id: 'E2', occurred_at: '2026-10-02T09:01:00.000+00:00', lane: 'simulator',
    level: 'critical', raw_news2: 9, repeat: false, patient_id: 'P001', ward: 'Ward A',
  },
  {
    id: 1, episode_id: 'E1', occurred_at: '2026-10-02T09:00:00.000+00:00', lane: 'manual',
    level: 'elevated', raw_news2: 5, repeat: true, patient_id: 'P002', ward: null,
  },
]

function response(over: Record<string, unknown> = {}) {
  return {
    known: true, unknown_reason: null, total: 68, alerts: ROWS,
    by_lane: { simulator: 60, manual: 8 }, scope: null, house_total: null, hours: 24,
    ...over,
  }
}

beforeEach(() => {
  apiFetch.mockReset()
})

describe('admin alerts tab', () => {
  it('renders alert.fired rows with their lane, level and patient', async () => {
    apiFetch.mockResolvedValue(response())
    render(<AlertOperations />)

    expect(await screen.findByText('68 alerts in the last 24 hours')).toBeTruthy()
    const table = screen.getByRole('table', { name: 'Alerts' })
    expect(table.textContent).toContain('Simulated feed')
    expect(table.textContent).toContain('Nurse observations')
    expect(table.textContent).toContain('Critical')
    expect(table.textContent).toContain('P001')
    expect(table.textContent).toContain('Repeat')
    expect(apiFetch.mock.calls[0][0]).toBe('/admin/alerts?limit=25&offset=0')
  })

  it('pages and filters on the server, not in the browser', async () => {
    apiFetch.mockResolvedValue(response())
    render(<AlertOperations />)
    await screen.findByText('68 alerts in the last 24 hours')

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(apiFetch).toHaveBeenLastCalledWith('/admin/alerts?limit=25&offset=25'))

    fireEvent.change(screen.getByLabelText('Lane'), { target: { value: 'manual' } })
    await waitFor(() => expect(apiFetch).toHaveBeenLastCalledWith('/admin/alerts?limit=25&offset=0&lane=manual'))
  })

  it('says unknown, not empty, when the alert log is off', async () => {
    apiFetch.mockResolvedValue(response({
      known: false, total: null, alerts: [], by_lane: {},
      unknown_reason: 'The shadow event log is switched off. This number is unknown, not zero.',
    }))
    render(<AlertOperations />)

    expect(await screen.findByText('Alert activity unknown')).toBeTruthy()
    expect(screen.getByText(/unknown, not zero/)).toBeTruthy()
    expect(screen.queryByText('No alerts')).toBeNull()
  })

  it('states the ward boundary for a scoped account', async () => {
    apiFetch.mockResolvedValue(response({ total: 0, alerts: [], scope: 'Cardiology', house_total: 12 }))
    render(<AlertOperations />)

    expect(await screen.findByText('0 alerts in the last 24 hours, Cardiology only (12 house-wide)')).toBeTruthy()
    expect(screen.getByText('No alerts in Cardiology')).toBeTruthy()
  })
})
