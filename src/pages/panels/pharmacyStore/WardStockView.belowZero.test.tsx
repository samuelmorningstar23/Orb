/**
 * Pharmacy store, Ward stock: a drug given on the eMAR from ward stock when
 * the ward's books held none (grn-17) is flagged on the pharmacy's list of
 * that ward, with the count it needs. On hand never turns negative.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WardStockView from './WardStockView'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: 'PHA2', name: 'Test user', role: 'pharmacist' }),
}))

function stockWith(belowZero: number | undefined) {
  return {
    today: '2026-10-20',
    stores: [{
      store: { id: 7, code: 'WS-KES', name: 'Kestrel ward stock', department: 'Kestrel',
        ward: 'KES-1', is_active: true },
      drugs: [{
        drug_id: 'MED-QRV', drug_name: 'Quorvexin 5 mg Tablet', form: 'Tablet',
        strength: '5 mg', dispense_unit: null, listed: true, par_units: 20, max_units: null,
        on_hand_units: 0, held_units: 0, expired_units: 0, in_transit_units: 0,
        requested_units: 0, earliest_expiry: null, below_par: true, top_up_units: 20,
        ...(belowZero === undefined ? {} : { below_zero_units: belowZero }),
        problem: null, lots: [],
      }],
    }],
    may_raise: false, may_receive: false, may_decide: true, may_issue: true,
    priorities: ['routine'],
  }
}

let stock: ReturnType<typeof stockWith> = stockWith(1)

beforeEach(() => {
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/pharmacy-store/ward/stock')) return stock
    if (path.startsWith('/pharmacy-store/ward/indents')) return { indents: [] }
    throw new Error(`unexpected ${path}`)
  })
})

afterEach(() => {
  vi.clearAllMocks()
})

function renderView() {
  render(<WardStockView meta={{ features: { pharmacy_ward_stock: true } }}
                        openView={() => {}} onModuleOff={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Ward stores' }))
}

describe('WardStockView below zero on the books', () => {
  it('flags a drug given with none on the books and asks for a count', async () => {
    stock = stockWith(3)
    renderView()
    expect(await screen.findByText('3 given with none on the books, count this drug'))
      .toBeInTheDocument()
  })

  it('shows nothing new when nothing is below zero', async () => {
    stock = stockWith(0)
    renderView()
    await screen.findAllByText('Quorvexin 5 mg Tablet')
    expect(screen.queryByText(/with none on the books/)).toBeNull()
  })

  it('reads an answer without the field as nothing below zero', async () => {
    stock = stockWith(undefined)
    renderView()
    await screen.findAllByText('Quorvexin 5 mg Tablet')
    expect(screen.queryByText(/with none on the books/)).toBeNull()
  })
})
