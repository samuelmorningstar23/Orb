/**
 * THE PATIENT FILTER MUST NOT RE-VERIFY THE AUDIT CHAIN ON EVERY KEYSTROKE.
 *
 * Every load hashes the whole audit table on the server. The filter used to
 * reload per keystroke with no debounce and no abort, so typing a patient id
 * piled up requests and a late hospital-wide answer could paint under the
 * filter. Rows also rendered six cells into a five-track grid when the chain
 * flag showed, so the timestamp printed over the next row.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import FlightRecorder, { FILTER_DEBOUNCE_MS } from './FlightRecorder'

const EVENTS = [
  {
    id: 1, ts: '2026-10-02T09:00:00+00:00', source: 'audit', actor: 'DOC001', action: 'chart_viewed',
    target: 'patient:P001', detail: null, chain_ok: true, patient_id: 'P001', correlation_id: 'c1',
  },
  {
    id: 'e1', ts: '2026-10-02T09:01:00+00:00', source: 'event', actor: 'vigil', action: 'patient_deteriorated',
    target: 'vigil.deterioration', detail: null, chain_ok: null, patient_id: 'P001', correlation_id: null,
  },
]

function timelineCalls(): string[] {
  return apiFetch.mock.calls.map((c) => c[0] as string).filter((p) => p.startsWith('/flight-recorder/timeline'))
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  apiFetch.mockReset()
  apiFetch.mockImplementation((path: string) => {
    if (path.startsWith('/flight-recorder/verify')) return Promise.resolve({ ok: true, broken_at: null, count: 1 })
    if (path.startsWith('/flight-recorder/timeline')) return Promise.resolve({ events: EVENTS, count: EVENTS.length })
    if (path.startsWith('/flight-recorder/event/')) return Promise.resolve({ source: 'audit', event: {} })
    return Promise.reject(new Error(`unexpected ${path}`))
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('flight recorder filter', () => {
  it('typing five characters issues one request', async () => {
    render(<FlightRecorder />)
    await screen.findByText('Chart viewed')
    expect(timelineCalls()).toHaveLength(1)

    const input = screen.getByLabelText('Filter by patient ID')
    for (const v of ['P', 'P0', 'P00', 'P001', 'P0011']) {
      fireEvent.change(input, { target: { value: v } })
    }
    expect(timelineCalls()).toHaveLength(1)

    await act(async () => { vi.advanceTimersByTime(FILTER_DEBOUNCE_MS) })
    await screen.findByText('Chart viewed')

    expect(timelineCalls()).toEqual([
      '/flight-recorder/timeline?limit=500',
      '/flight-recorder/timeline?patient_id=P0011&limit=500',
    ])
  })

  it('Enter commits the filter at once, without waiting for the pause', async () => {
    render(<FlightRecorder />)
    await screen.findByText('Chart viewed')

    const input = screen.getByLabelText('Filter by patient ID')
    fireEvent.change(input, { target: { value: 'P001' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await screen.findByText('Chart viewed')

    expect(timelineCalls()).toContain('/flight-recorder/timeline?patient_id=P001&limit=500')
    await act(async () => { vi.advanceTimersByTime(FILTER_DEBOUNCE_MS) })
    expect(timelineCalls()).toHaveLength(2)
  })

  it('a late hospital-wide answer never paints under a patient filter', async () => {
    let releaseHouse: (v: unknown) => void = () => {}
    apiFetch.mockImplementation((path: string) => {
      if (path.startsWith('/flight-recorder/verify')) return Promise.resolve({ ok: true, broken_at: null, count: 1 })
      if (path === '/flight-recorder/timeline?limit=500') {
        return new Promise((resolve) => { releaseHouse = resolve })
      }
      if (path.startsWith('/flight-recorder/timeline')) return Promise.resolve({ events: EVENTS, count: EVENTS.length })
      return Promise.resolve({ source: 'audit', event: {} })
    })
    render(<FlightRecorder />)
    await act(async () => {})
    const firstSignal = (apiFetch.mock.calls.find((c) => c[0] === '/flight-recorder/timeline?limit=500')![1] as RequestInit).signal

    const input = screen.getByLabelText('Filter by patient ID')
    fireEvent.change(input, { target: { value: 'P001' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await screen.findByText('Chart viewed')
    expect(firstSignal?.aborted).toBe(true)

    const houseRow = { ...EVENTS[0], id: 99, action: 'hospital_wide_row', patient_id: 'P777' }
    await act(async () => { releaseHouse({ events: [houseRow], count: 1 }) })

    expect(screen.queryByText('hospital_wide_row')).toBeNull()
    expect(screen.getByText('Chart viewed')).toBeTruthy()
  })
})

describe('flight recorder timeline rows', () => {
  it('renders the same six grid cells on every row, flagged or not', async () => {
    const { container } = render(<FlightRecorder />)
    await screen.findByText('Chart viewed')

    const rows = container.querySelectorAll('.fr-tl-item')
    expect(rows).toHaveLength(2)
    rows.forEach((row) => expect(row.children).toHaveLength(6))
  })
})
