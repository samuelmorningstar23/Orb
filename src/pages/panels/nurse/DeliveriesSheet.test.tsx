/**
 * WHAT THE DELIVERIES TAB IS ALLOWED TO SAY.
 *
 * This screen is the ward end of a pharmacy dispense: a nurse signs, in her own
 * name, for a drug that physically arrived. Two ways it can be wrong, and both
 * are about medication sitting in a tray outside a door:
 *
 *   · A FAILED READ THAT LOOKS LIKE A QUIET WARD. "Nothing in transit" and "we
 *     could not find out what is in transit" are opposite facts. If a failure
 *     paints the calm empty state, a nurse walks away from drugs that are
 *     waiting for her. This is the same rule the shift board follows (§6.1).
 *   · A DISPATCH WITH NO NAME ON IT. She is attesting against a specific
 *     dispense by a specific person. That is the whole point of the step —
 *     pharmacy cannot sign for its own dispatch — and it is invisible if the
 *     row does not say whose dispatch it is.
 */
import { render, screen, act, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getOrdersAwaitingReceipt = vi.fn()
const receiveHelixOrder = vi.fn()

vi.mock('../../../api/client', () => ({
  getOrdersAwaitingReceipt: (...a: unknown[]) => getOrdersAwaitingReceipt(...a),
  receiveHelixOrder: (...a: unknown[]) => receiveHelixOrder(...a),
}))

import DeliveriesSheet from './DeliveriesSheet'

const ORDER = {
  id: 'ORD-1',
  patient_id: 'PAT-1',
  patient_name: 'Anita Rao',
  medication_name: 'Paracetamol',
  strength: '500mg',
  dosage: '500mg',
  route: 'PO',
  frequency: 'TDS',
  ward: 'GM-1',
  bed: 'B-3',
  dispensed_by: 'PHA001',
  dispensed_by_name: 'Meera Nair',
  dispensed_at: '2026-09-07T09:14:00Z',
}

beforeEach(() => {
  getOrdersAwaitingReceipt.mockReset()
  receiveHelixOrder.mockReset()
})
afterEach(() => vi.clearAllMocks())

const renderSheet = async () => {
  await act(async () => { render(<DeliveriesSheet />) })
}

describe('Deliveries', () => {
  it('names the drug, the patient and who dispensed it', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [ORDER] })
    await renderSheet()
    expect(screen.getByText(/Paracetamol 500mg/)).toBeTruthy()
    expect(screen.getByText(/Anita Rao/)).toBeTruthy()
    expect(screen.getByText(/Dispensed by Meera Nair/)).toBeTruthy()
  })

  it('does not repeat a strength the formulary name already carries', async () => {
    // Seen on the ward, 2026-09-07: "Paracetamol 650mg Oral Tablet 650mg".
    // Most helix_medication_master rows name the strength inside `name`.
    getOrdersAwaitingReceipt.mockResolvedValue({
      orders: [{ ...ORDER, medication_name: 'Paracetamol 650mg Oral Tablet', strength: '650mg' }],
    })
    await renderSheet()
    expect(screen.getByText('Paracetamol 650mg Oral Tablet')).toBeTruthy()
    expect(screen.queryByText(/650mg Oral Tablet 650mg/)).toBeNull()
  })

  it('a failed read never renders as a quiet ward', async () => {
    // THE ASSERTION THAT MATTERS. Not "an error appeared" but "the reassurance
    // did NOT" — a screen can show both and still be lying, and it is the
    // reassurance a nurse acts on.
    getOrdersAwaitingReceipt.mockRejectedValue(new Error('network'))
    await renderSheet()
    expect(screen.getByRole('alert').textContent).toMatch(/could not load/i)
    expect(screen.queryByText(/Nothing in transit/i)).toBeNull()
    expect(screen.getByText(/does not mean nothing is in transit/i)).toBeTruthy()
  })

  it('an empty queue reads as reassurance, not as a failure', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
    await renderSheet()
    expect(screen.getByText(/Nothing in transit/i)).toBeTruthy()
    // The evidence is when it was checked, not a double negative.
    expect(screen.getByText(/^Checked at/).textContent).toMatch(/^Checked at \d{1,2}[:.]\d{2}/)
    expect(screen.queryByText(/Nothing failed to load/i)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('confirming receipt posts and refreshes the queue', async () => {
    getOrdersAwaitingReceipt
      .mockResolvedValueOnce({ orders: [ORDER] })
      .mockResolvedValueOnce({ orders: [] })
    receiveHelixOrder.mockResolvedValue({ ok: true })
    await renderSheet()

    await act(async () => { screen.getByText('Confirm receipt').click() })
    expect(receiveHelixOrder).toHaveBeenCalledWith('ORD-1')
    await waitFor(() => expect(screen.getByText(/Nothing in transit/i)).toBeTruthy())
  })

  it('a refused receipt says so and leaves the row in the queue', async () => {
    // The server refuses when the receiving account is the dispatching account.
    // The row must STAY: the drug is still unsigned-for.
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [ORDER] })
    receiveHelixOrder.mockRejectedValue(
      new Error('The account that dispensed this order cannot also record that the ward received it.'))
    await renderSheet()

    await act(async () => { screen.getByText('Confirm receipt').click() })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/cannot also record/i))
    expect(screen.getByText(/Paracetamol 500mg/)).toBeTruthy()
  })

  it('a missing dose reads as missing, never as a dose', async () => {
    // The Relay handoff writes "REVIEW REQUIRED" when Sage extracted a drug but
    // no reliable dose. Anything that renders that as if it were a regimen is
    // the defect the string exists to prevent.
    getOrdersAwaitingReceipt.mockResolvedValue({
      orders: [{ ...ORDER, dosage: null, route: null, frequency: null }],
    })
    await renderSheet()
    expect(screen.getByText(/No dose recorded/i)).toBeTruthy()
  })

  it('names the substitute that was dispensed, with the prescribed drug and the note', async () => {
    // The nurse signs for what is in the tray. With a substitute on the order
    // the row used to name the prescribed drug instead.
    getOrdersAwaitingReceipt.mockResolvedValue({
      orders: [{
        ...ORDER,
        medication_name: 'Monocef 1g Injection',
        strength: '1g',
        original_medication_name: 'Cefadin 500mg Capsule',
        pharmacist_notes: 'Out of stock, prescriber agreed',
      }],
    })
    await renderSheet()
    expect(screen.getByText('Monocef 1g Injection (substituted for Cefadin 500mg Capsule)')).toBeTruthy()
    expect(screen.getByText('Pharmacist note: Out of stock, prescriber agreed')).toBeTruthy()
  })
})
