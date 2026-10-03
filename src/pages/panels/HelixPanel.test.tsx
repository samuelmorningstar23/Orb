/**
 * Helix keeps its work queue fresh.
 *
 * It loaded once per visit, so an order a clinician approved after the
 * pharmacist opened the tab never appeared. It now polls every 30 s, has a
 * Refresh button, and a failed refresh keeps the lists on screen while the
 * header says they may be out of date.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string) => apiFetch(path),
  getHelixOrderSafety: vi.fn(),
  isSafetyAckRequired: () => false,
  transitionHelixOrder: vi.fn(),
}))

import HelixPanel from './HelixPanel'

function pendingOrder(id: string, name: string) {
  return {
    id, patient_id: `P-${id}`, patient_name: name, medication_name: `Drug ${id}`,
    dosage: '1 tab', frequency: 'OD', route: 'Oral', status: 'pending',
    created_at: '2026-10-03 04:00:00', ward: 'GM', bed: '1',
  }
}

let orders: unknown[] = []
let failOrders = false

beforeEach(() => {
  orders = [pendingOrder('A', 'Asha Rao')]
  failOrders = false
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string) => {
    if (path === '/helix/orders') {
      if (failOrders) throw new Error('Network down')
      return { orders }
    }
    if (path === '/helix/inventory') return { inventory: [] }
    if (path === '/helix/catalog') return { catalog: [] }
    throw new Error(`unexpected ${path}`)
  })
})

afterEach(() => {
  vi.useRealTimers()
})

const orderCalls = () => apiFetch.mock.calls.filter(([p]) => p === '/helix/orders').length

describe('HelixPanel refresh', () => {
  it('Refresh fetches the queue again and shows the new order', async () => {
    render(<HelixPanel />)
    expect(await screen.findByText('Asha Rao')).toBeInTheDocument()
    expect(orderCalls()).toBe(1)

    orders = [pendingOrder('A', 'Asha Rao'), pendingOrder('B', 'Vikram Shah')]
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Vikram Shah')).toBeInTheDocument()
    expect(orderCalls()).toBe(2)
  })

  it('polls every 30 seconds without a click', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<HelixPanel />)
    expect(await screen.findByText('Asha Rao')).toBeInTheDocument()
    orders = [pendingOrder('B', 'Vikram Shah')]
    await act(async () => { vi.advanceTimersByTime(30_000) })
    await waitFor(() => expect(screen.getByText('Vikram Shah')).toBeInTheDocument())
    expect(orderCalls()).toBe(2)
  })

  it('a failed refresh keeps the queue on screen and says it may be out of date', async () => {
    render(<HelixPanel />)
    expect(await screen.findByText('Asha Rao')).toBeInTheDocument()
    failOrders = true
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(/the last refresh failed, so these lists may be out of date/)).toBeInTheDocument()
    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
    expect(screen.queryByText('Pharmacy queue did not load.')).not.toBeInTheDocument()
  })

  it('a failed first load says so rather than showing an empty queue', async () => {
    failOrders = true
    render(<HelixPanel />)
    expect(await screen.findByText('Pharmacy queue did not load.')).toBeInTheDocument()
    expect(screen.queryByText('Queue clear, no pending orders.')).not.toBeInTheDocument()
  })
})

describe('HelixPanel views', () => {
  it('are a tablist: one selected tab, arrow keys move the selection', async () => {
    render(<HelixPanel />)
    await screen.findByText('Asha Rao')
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map(t => t.textContent)).toEqual(['Overview', 'Patient Board', 'Formulary'])
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
    expect(tabs[1]).toHaveAttribute('tabindex', '-1')

    fireEvent.keyDown(tabs[0], { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'Patient Board' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel', { name: 'Patient Board' })).toBeInTheDocument()
    expect(screen.getByText('Patient Medication Board')).toBeInTheDocument()
  })
})
