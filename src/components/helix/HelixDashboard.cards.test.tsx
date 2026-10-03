/**
 * Helix overview cards.
 *
 * "Delivered, completed today" counted every delivery ever made, so the number
 * only ever grew. It now counts orders the ward signed for on today's local
 * date. "Rejected" was a count with nowhere to go; it now opens the list.
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import HelixDashboard from './HelixDashboard'

/** A bare UTC 'YYYY-MM-DD HH:MM:SS' string, the shape SQLite stores. */
function utcStamp(d: Date): string {
  return d.toISOString().slice(0, 19).replace('T', ' ')
}

function order(id: string, status: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    patient_id: `P-${id}`,
    patient_name: `Patient ${id}`,
    medication_name: `Drug ${id}`,
    dosage: '1 tab',
    frequency: 'OD',
    route: 'Oral',
    status,
    created_at: utcStamp(new Date()),
    prescribed_by_name: 'Dr Mehta',
    ward: 'GM',
    bed: '1',
    ...extra,
  }
}

function deliveredCard(): HTMLElement {
  return screen.getByText('Received on the ward today').parentElement as HTMLElement
}

describe('Delivered card', () => {
  it('counts only orders received on the ward today, not every delivery ever made', () => {
    const now = new Date()
    const lastWeek = new Date(now.getTime() - 7 * 24 * 3600 * 1000)
    render(<HelixDashboard onSelectOrder={() => {}} orders={[
      order('A', 'delivered', { received_at: utcStamp(now) }),
      order('B', 'delivered', { received_at: now.toISOString() }),
      order('C', 'delivered', { received_at: utcStamp(lastWeek) }),
      // Delivered before ward receipt existed: no day to count it on.
      order('D', 'delivered', { received_at: null }),
      // Not delivered at all.
      order('E', 'dispensed'),
    ]} />)
    expect(within(deliveredCard()).getByText('2')).toBeInTheDocument()
  })

  it('reads a bare database timestamp as UTC, so the day boundary is the local one', () => {
    // Local midnight today, written as the UTC instant SQLite would store.
    const startOfToday = new Date()
    startOfToday.setHours(0, 0, 0, 0)
    const justBefore = new Date(startOfToday.getTime() - 60 * 1000)
    const justAfter = new Date(startOfToday.getTime() + 60 * 1000)
    render(<HelixDashboard onSelectOrder={() => {}} orders={[
      order('A', 'delivered', { received_at: utcStamp(justBefore) }),
      order('B', 'delivered', { received_at: utcStamp(justAfter) }),
    ]} />)
    expect(within(deliveredCard()).getByText('1')).toBeInTheDocument()
  })
})

describe('Rejected card', () => {
  it('is not shown when nothing is rejected', () => {
    render(<HelixDashboard onSelectOrder={() => {}} orders={[order('A', 'pending')]} />)
    expect(screen.queryByRole('button', { name: /Rejected/ })).not.toBeInTheDocument()
  })

  it('opens the list of rejected orders with the reason, and each row opens the order', () => {
    const onSelect = vi.fn()
    render(<HelixDashboard onSelectOrder={onSelect} orders={[
      order('A', 'rejected', { pharmacist_notes: 'Duplicate therapy', reviewed_at: utcStamp(new Date()) }),
      order('B', 'rejected', { pharmacist_notes: null }),
      order('C', 'pending'),
    ]} />)

    const card = screen.getByRole('button', { name: /Rejected/ })
    expect(card).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Rejected Orders')).not.toBeInTheDocument()

    fireEvent.click(card)
    expect(card).toHaveAttribute('aria-expanded', 'true')
    const list = document.getElementById('helix-rejected-orders') as HTMLElement
    expect(within(list).getByText('Rejected Orders')).toBeInTheDocument()
    expect(within(list).getByText('Duplicate therapy')).toBeInTheDocument()
    expect(within(list).getByText('No reason recorded')).toBeInTheDocument()
    // No review time on the row: said, not left as a blank cell.
    expect(within(list).getByText('Not recorded')).toBeInTheDocument()
    expect(within(list).queryByText('Patient C')).not.toBeInTheDocument()

    fireEvent.click(within(list).getByRole('button', { name: 'Open rejected order: Drug A for Patient A' }))
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'A' }))

    fireEvent.click(card)
    expect(screen.queryByText('Rejected Orders')).not.toBeInTheDocument()
  })
})
