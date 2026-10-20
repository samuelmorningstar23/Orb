/**
 * Ward stock used on a dose when the books held none (grn-17): the ward's
 * stock list says so, in words, and asks for a count. It is a flag on the
 * drug, never a refusal and never a negative "on hand".
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../../api/client', () => ({
  getUser: () => ({ id: 'NUR-1', role: 'nurse' }),
}))

const getWardStock = vi.fn()
const getWardTransfersAwaiting = vi.fn()
const listWardIndents = vi.fn()

vi.mock('../../../api/pharmacyStore/ward', () => ({
  WARD_INDENT_PRIORITY_LABELS: { routine: 'Routine', urgent: 'Urgent', emergency: 'Emergency' },
  getWardStock: (...a: unknown[]) => getWardStock(...a),
  getWardTransfersAwaiting: (...a: unknown[]) => getWardTransfersAwaiting(...a),
  listWardIndents: (...a: unknown[]) => listWardIndents(...a),
  raiseWardIndent: vi.fn(),
  receiveWardTransfer: vi.fn(),
  cancelWardIndent: vi.fn(),
}))

import WardStockSection from './WardStockSection'
import {
  resetFeatureFlags, resetModuleFlags, setFeatureFlags, setModuleFlags,
} from '../../../shellConfig'

const STORE = {
  id: 7, code: 'WS-KES', name: 'Kestrel ward stock', department: 'Kestrel',
  ward: 'KES-1', is_active: true,
}

function drug(overrides: Record<string, unknown> = {}) {
  return {
    drug_id: 'MED-QRV', drug_name: 'Quorvexin 5 mg Tablet', form: 'Tablet', strength: '5 mg',
    dispense_unit: null, listed: true, par_units: 20, max_units: null, on_hand_units: 0,
    held_units: 0, expired_units: 0, in_transit_units: 0, requested_units: 0,
    earliest_expiry: null, below_par: true, top_up_units: 20, problem: null, lots: [],
    ...overrides,
  }
}

function answer(d: ReturnType<typeof drug>) {
  getWardStock.mockResolvedValue({
    today: '2026-10-20', stores: [{ store: STORE, drugs: [d] }],
    may_raise: false, may_receive: false, may_decide: false, may_issue: false,
    priorities: ['routine'],
  })
  getWardTransfersAwaiting.mockResolvedValue({ transfers: [] })
  listWardIndents.mockResolvedValue({ indents: [] })
}

beforeEach(() => {
  for (const fn of [getWardStock, getWardTransfersAwaiting, listWardIndents]) fn.mockReset()
  act(() => {
    setModuleFlags({ 'pharmacy-store': true })
    setFeatureFlags({ pharmacy_ward_stock: true })
  })
})
afterEach(() => {
  vi.clearAllMocks()
  act(() => { resetModuleFlags(); resetFeatureFlags() })
})

describe('Ward stock below zero on the books', () => {
  it('asks the ward to count a drug given with none on the books', async () => {
    answer(drug({ below_zero_units: 2 }))
    await act(async () => { render(<WardStockSection />) })
    await waitFor(() => expect(screen.getByText('Quorvexin 5 mg Tablet')).toBeTruthy())
    expect(screen.getByText('2 given with none on the books, count this drug')).toBeTruthy()
    // On hand stays what can be used: never a negative number.
    expect(screen.getByText(/0 units on hand of 20/)).toBeTruthy()
  })

  it('says nothing new when the books are not below zero', async () => {
    answer(drug({ on_hand_units: 6, below_zero_units: 0 }))
    await act(async () => { render(<WardStockSection />) })
    await waitFor(() => expect(screen.getByText('Quorvexin 5 mg Tablet')).toBeTruthy())
    expect(screen.queryByText(/with none on the books/)).toBeNull()
  })

  it('reads an answer without the field as nothing below zero', async () => {
    answer(drug({ on_hand_units: 6 }))
    await act(async () => { render(<WardStockSection />) })
    await waitFor(() => expect(screen.getByText('Quorvexin 5 mg Tablet')).toBeTruthy())
    expect(screen.queryByText(/with none on the books/)).toBeNull()
  })
})
