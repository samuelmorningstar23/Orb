/**
 * The Deliveries tab names the order behind a tray (medorders-10) and lets the
 * ward send unused medicines back to the pharmacy (grn-15).
 *
 * Pinned:
 *   * a tray linked to a medication order says the order's status in words,
 *     and what to do when that order is on hold or has ended; an unlinked
 *     tray reads exactly as before;
 *   * 'Return to pharmacy' renders nothing and asks the server nothing unless
 *     the pharmacy store is on and the account holds ward_return.record;
 *   * a return names the order and batch, the units (never more than can come
 *     back), the seal, and one request key reused on a retry;
 *   * a narcotic or psychotropic medicine is sent to the counter, not returned
 *     from here; a failed read is said, never drawn as nothing to return.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getOrdersAwaitingReceipt = vi.fn()
let signedInRole = 'nurse'

vi.mock('../../../api/client', () => ({
  getOrdersAwaitingReceipt: (...a: unknown[]) => getOrdersAwaitingReceipt(...a),
  receiveHelixOrder: vi.fn(),
  getUser: () => ({ id: 'NUR-1', role: signedInRole }),
  apiFetch: vi.fn(),
  socket: { on: vi.fn(), off: vi.fn() },
}))

const getReturnableOrders = vi.fn()
const listWardReturns = vi.fn()
const getWardReturnOptions = vi.fn()
const recordWardReturn = vi.fn()

vi.mock('../../../api/pharmacyStore/returns', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api/pharmacyStore/returns')>()
  let n = 0
  return {
    ...actual,
    getReturnableOrders: (...a: unknown[]) => getReturnableOrders(...a),
    listWardReturns: (...a: unknown[]) => listWardReturns(...a),
    getWardReturnOptions: (...a: unknown[]) => getWardReturnOptions(...a),
    recordWardReturn: (...a: unknown[]) => recordWardReturn(...a),
    newWardReturnKey: () => `key-${++n}`,
  }
})

import DeliveriesSheet from './DeliveriesSheet'
import { resetFeatureFlags, resetModuleFlags, setModuleFlags } from '../../../shellConfig'

const TRAY = {
  id: 'HX-1', patient_id: 'P-1', patient_name: 'Anita Rao', medication_name: 'Enoxaparin',
  strength: '40 mg', dosage: '40 mg', route: 'SC', frequency: 'OD', ward: 'GM-1', bed: 'B-3',
  dispensed_by: 'PH1', dispensed_by_name: 'Meera Nair', dispensed_at: '2026-10-09T09:14:00Z',
}

const BATCH = {
  lot_id: 'LOT-1', drug_id: 'MED-1', drug_name: 'Amoxicillin', strength: '500 mg', form: 'capsule',
  dispense_unit: 'capsules', batch_no: 'B77', expiry_date: '2027-01-01', controlled: false,
  dispensed_units: 6, returned_units: 2, returnable_units: 4,
}

const RETURNABLE = {
  id: 'HX-9', patient_id: 'P-2', patient_name: 'Ravi Kumar', bed: 'B-5', ward: 'GM-1', mrn: 'M2',
  dosage: '500 mg', route: 'PO', frequency: 'TDS', received_by: 'NUR-1', received_by_name: 'Nurse One',
  received_at: '2026-10-09T10:00:00Z', created_at: '2026-10-09T08:00:00Z', med_order_id: null,
  batches: [BATCH],
}

const OPTIONS = {
  stores: [{ code: 'RQ1', name: 'Returns quarantine' }], restock_allowed: false, lane: 'ward',
  may_record_controlled: false, witnesses: [], decisions: ['write_off', 'restock'],
  status_labels: {}, number_series: 'PHRET',
}

beforeEach(() => {
  signedInRole = 'nurse'
  for (const fn of [getOrdersAwaitingReceipt, getReturnableOrders, listWardReturns,
    getWardReturnOptions, recordWardReturn]) fn.mockReset()
  getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
  getReturnableOrders.mockResolvedValue({ orders: [RETURNABLE], window_days: 30 })
  listWardReturns.mockResolvedValue({ ward_returns: [] })
  getWardReturnOptions.mockResolvedValue(OPTIONS)
})

afterEach(() => {
  cleanup()
  resetModuleFlags()
  resetFeatureFlags()
})

describe('DeliveriesSheet names the order behind a tray', () => {
  it('says a linked order is on hold, and leaves an unlinked tray as it was', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [
      { ...TRAY, linked_order: { id: 'MO-1', status: 'held', version: 4, verified_by: null, verified_at: null } },
      { ...TRAY, id: 'HX-2', medication_name: 'Paracetamol' },
    ] })
    render(<DeliveriesSheet />)
    const held = (await screen.findByText(/Enoxaparin/)).closest('li') as HTMLElement
    expect(within(held).getByText('For the medication order: on hold')).toBeTruthy()
    expect(within(held).getByText(/put this medicine on hold\. Sign for what arrived/)).toBeTruthy()
    const plain = screen.getByText(/Paracetamol/).closest('li') as HTMLElement
    expect(within(plain).queryByText(/For the medication order/)).toBeNull()
  })

  it('says an ended order goes back to the pharmacy', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [
      { ...TRAY, linked_order: { id: 'MO-1', status: 'stopped', version: 6, verified_by: null, verified_at: null } },
    ] })
    render(<DeliveriesSheet />)
    expect(await screen.findByText('For the medication order: stopped')).toBeTruthy()
    expect(screen.getByText(/This order has ended\. Sign for what arrived, then send it back/)).toBeTruthy()
  })
})

describe('Return to pharmacy (grn-15)', () => {
  it('renders nothing and asks nothing while the pharmacy store is off', async () => {
    render(<DeliveriesSheet />)
    await screen.findByText('Nothing in transit.')
    expect(screen.queryByRole('heading', { name: 'Return to pharmacy' })).toBeNull()
    expect(getReturnableOrders).not.toHaveBeenCalled()
  })

  it('renders nothing for an account without ward_return.record', async () => {
    setModuleFlags({ 'pharmacy-store': true })
    signedInRole = 'doctor'
    render(<DeliveriesSheet />)
    await screen.findByText('Nothing in transit.')
    expect(screen.queryByRole('heading', { name: 'Return to pharmacy' })).toBeNull()
    expect(getReturnableOrders).not.toHaveBeenCalled()
  })

  it('records a return with the units, the seal and one key reused on a retry', async () => {
    setModuleFlags({ 'pharmacy-store': true })
    recordWardReturn.mockRejectedValueOnce(Object.assign(new Error('Server unreachable.'), { status: 0 }))
    recordWardReturn.mockResolvedValueOnce({ ward_return: { id: 1 } })
    render(<DeliveriesSheet />)
    const row = (await screen.findByText('Amoxicillin 500 mg')).closest('li') as HTMLElement
    expect(within(row).getByText('4 capsules can go back')).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Return to pharmacy' }))
    const units = within(row).getByLabelText('Units coming back') as HTMLInputElement
    expect(units.value).toBe('4')
    fireEvent.change(units, { target: { value: '5' } })
    expect((within(row).getByRole('button', { name: 'Record return' }) as HTMLButtonElement).disabled)
      .toBe(true)
    expect(within(row).getByText('Enter a whole number from 1 to 4.')).toBeTruthy()
    fireEvent.change(units, { target: { value: '3' } })
    fireEvent.click(within(row).getByRole('radio', { name: 'Sealed' }))
    await act(async () => { fireEvent.click(within(row).getByRole('button', { name: 'Record return' })) })
    expect(await within(row).findByRole('alert')).toBeTruthy()
    await act(async () => { fireEvent.click(within(row).getByRole('button', { name: 'Record return' })) })
    await waitFor(() => expect(recordWardReturn).toHaveBeenCalledTimes(2))
    const [body, firstKey] = recordWardReturn.mock.calls[0]
    expect(body).toEqual({ helix_order_id: 'HX-9', quantity_units: 3, lot_id: 'LOT-1',
      seal_intact: true, note: null, store_code: 'RQ1' })
    expect(recordWardReturn.mock.calls[1][1]).toBe(firstKey)
    expect(await screen.findByText(/Return recorded in your name: 3 capsules of Amoxicillin 500 mg/))
      .toBeTruthy()
  })

  it('sends a controlled medicine to the counter instead of offering the button', async () => {
    setModuleFlags({ 'pharmacy-store': true })
    getReturnableOrders.mockResolvedValue({ orders: [{ ...RETURNABLE,
      batches: [{ ...BATCH, drug_name: 'Morphine', strength: '10 mg', controlled: true }] }],
    window_days: 30 })
    render(<DeliveriesSheet />)
    const row = (await screen.findByText('Morphine 10 mg')).closest('li') as HTMLElement
    expect(within(row).queryByRole('button', { name: 'Return to pharmacy' })).toBeNull()
    expect(within(row).getByText(/take it to the pharmacy counter/)).toBeTruthy()
  })

  it('says a failed read, never that there is nothing to return', async () => {
    setModuleFlags({ 'pharmacy-store': true })
    getReturnableOrders.mockRejectedValue(Object.assign(new Error('Server unreachable.'), { status: 0 }))
    render(<DeliveriesSheet />)
    expect(await screen.findByText('Could not load returns.')).toBeTruthy()
    expect(screen.queryByText(/has units left to return/)).toBeNull()
  })
})
