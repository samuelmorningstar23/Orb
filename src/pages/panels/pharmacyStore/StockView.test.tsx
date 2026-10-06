/**
 * Pharmacy store, Stock (grn-9): what the screen must get right.
 *
 *  1. The tiles lead with the server's numbers, and the orders tile opens
 *     Purchasing.
 *  2. Stock rows show dispensable, usable, in transit, quarantined and expired
 *     units as the server split them.
 *  3. A lot with no recorded cost reads 'Rate not configured', never Rs 0.
 *  4. A drug with no reorder level reads 'No reorder level set', never 0.
 *  5. Near expiry shows its values at cost and MRP with the server's labels.
 *  6. The ledger pages back by seq, and a patient-linked row shows only that
 *     it was one.
 *  7. A 'module_disabled' answer hands back to the panel.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import StockView, { viewId, viewLabel, viewOrder, viewPermission } from './StockView'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: 'PHA1', name: 'Test user', role: 'pharmacist' }),
}))

const OVERVIEW = {
  today: '2026-10-04', below_reorder_level: 2, without_reorder_level: 5, near_expiry_days: 90,
  near_expiry_lots: 3, near_expiry_units: 40, expired_on_shelf_units: 7, quarantined_units: 4,
  in_transit_units: 6, dispensable_units: 900, legacy_lots: 0,
  purchase_orders: { draft: 1, awaiting_approval: 2, approved: 0, open_with_vendor: 1 },
}

const STOCK_ROW = {
  store_id: 3, store_code: 'SUB1', store_label: 'Ward sub-store', store_kind: 'sub_store',
  store_kind_label: 'Sub-store', store_active: true, drug_id: 'MED-PARA',
  drug_name: 'Paracetamol 650mg Oral Tablet', generic_name: 'Paracetamol', form: 'Tablet',
  strength: '650mg', dispense_unit: null, on_hand_units: 19, dispensable_units: 0,
  usable_units: 10, expired_units: 5, undated_units: 2, quarantined_units: 4,
  in_transit_units: 6, other_units: 0, lot_count: 4,
}

function lot(overrides: Record<string, unknown> = {}) {
  return {
    id: 'LOT-1', drug_id: 'MED-PARA', drug_name: 'Paracetamol 650mg Oral Tablet', form: 'Tablet',
    strength: '650mg', dispense_unit: null, lot_number: 'PB-1', expiration_date: '2026-11-01',
    expiry_state: 'near_expiry', quantity: 5, status: 'available', status_label: 'Available',
    dispensable: false, store_id: 3, store_code: 'SUB1', store_label: 'Ward sub-store',
    store_kind: 'sub_store', units_per_pack: null, unit_cost_paise: null, mrp_pack_paise: null,
    cost_value_paise: null, cost_label: 'Rate not configured', mrp_value_paise: null,
    mrp_label: 'MRP not recorded', ...overrides,
  }
}

function reorderRow(overrides: Record<string, unknown> = {}) {
  return {
    store_id: 1, store_code: 'DISP', store_label: 'Main pharmacy', store_kind: 'dispensing',
    drug_id: 'MED-ATEN', drug_name: 'Atenolol 50mg Oral Tablet', form: 'Tablet', strength: '50mg',
    dispense_unit: null, pack_size_units: 10, purchase_uom: 'strip', ndps: false, basis: 'none',
    basis_label: 'No reorder level set', reorder_level_units: null,
    level_label: 'No reorder level set', target_units: null, stock_units: 12,
    in_transit_units: 0, on_order_units: 0, position_units: null, needs_reorder: false,
    suggested_units: null, suggested_packs: null, note: null, ...overrides,
  }
}

function movement(seq: number, overrides: Record<string, unknown> = {}) {
  return {
    id: `MV-${seq}`, seq, occurred_at: '2026-10-04T05:30:00Z', movement_type: 'adjustment',
    movement_label: 'Adjustment', quantity_change: -1, balance_after_units: seq,
    lot_id: 'LOT-1', lot_number: 'PB-1', expiration_date: '2026-11-01', drug_id: 'MED-PARA',
    drug_name: 'Paracetamol 650mg Oral Tablet', form: 'Tablet', store_id: 3,
    store_label: 'Ward sub-store', actor_id: 'PHA1', actor_name: 'Pharmacist One',
    reference_type: null, reference_id: null, reference_label: null, patient_linked: false,
    remarks: null, ...overrides,
  }
}

function routeApi(overrides: Record<string, (path: string) => unknown> = {}) {
  apiFetch.mockImplementation(async (path: string) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path)
    }
    if (path.startsWith('/pharmacy-store/stock/overview')) return OVERVIEW
    if (path.startsWith('/pharmacy-store/stock/lots')) {
      return { today: '2026-10-04', near_expiry_days: 90, lots: [lot()] }
    }
    if (path.startsWith('/pharmacy-store/stock')) {
      return { today: '2026-10-04', rows: [STOCK_ROW], totals: {} }
    }
    if (path.startsWith('/pharmacy-store/reorder')) return { today: '2026-10-04', rows: [reorderRow()] }
    if (path.startsWith('/pharmacy-store/near-expiry')) {
      return {
        today: '2026-10-04', days: 90, cutoff: '2027-01-02',
        lots: [lot({ id: 'LOT-2', lot_number: 'PB-2', cost_value_paise: 100000, cost_label: null,
          mrp_value_paise: 50000, mrp_label: null }), lot()],
        expired: [],
        totals: {
          near_expiry: { lots: 2, units: 10, cost_value_paise: 100000, cost_unpriced_lots: 1,
            mrp_value_paise: 50000, mrp_unpriced_lots: 1 },
          expired: { lots: 0, units: 0, cost_value_paise: 0, cost_unpriced_lots: 0,
            mrp_value_paise: 0, mrp_unpriced_lots: 0 },
        },
      }
    }
    if (path.startsWith('/pharmacy-store/ledger')) {
      return path.includes('before_seq=')
        ? { movements: [movement(1)], has_more: false, next_before_seq: null }
        : { movements: [movement(3, { movement_type: 'dispense', movement_label: 'Dispensed',
            reference_type: 'helix_order', reference_label: "A patient's medicine order",
            patient_linked: true }), movement(2)], has_more: true, next_before_seq: 2 }
    }
    throw new Error(`unexpected ${path}`)
  })
}

function renderView() {
  const onModuleOff = vi.fn()
  const openView = vi.fn()
  render(<StockView meta={{ settings: { near_expiry_days: 90 } }} openView={openView}
    onModuleOff={onModuleOff} />)
  return { onModuleOff, openView }
}

beforeEach(() => { apiFetch.mockReset() })
afterEach(() => { vi.restoreAllMocks() })

describe('StockView', () => {
  it('declares its tab', () => {
    expect([viewId, viewLabel, viewOrder, viewPermission])
      .toEqual(['stock', 'Stock', 40, 'pharmacy_store.read'])
  })

  it('leads with the server tiles, and the orders tile opens Purchasing', async () => {
    routeApi()
    const { openView } = renderView()
    const tiles = await screen.findByRole('list', { name: 'Stock summary' })
    expect(within(tiles).getByText('Lots expiring within 90 days')).toBeInTheDocument()
    expect(within(tiles).getByText('Drugs with no reorder level set')).toBeInTheDocument()
    fireEvent.click(within(tiles).getByText('Orders awaiting approval'))
    expect(openView).toHaveBeenCalledWith('purchasing')
  })

  it('shows the buckets as the server split them, and a lot with no cost in words', async () => {
    routeApi()
    renderView()
    const row = (await screen.findByText('Ward sub-store', { selector: '.psw-strong' })).closest('tr')!
    const cells = within(row).getAllByRole('cell').map(c => c.textContent)
    expect(cells.slice(2, 7)).toEqual(['0', '10', '6', '4', '5'])
    fireEvent.click(within(row).getByRole('button', { name: 'Lots' }))
    expect(await screen.findByText('Rate not configured')).toBeInTheDocument()
    expect(screen.getByText('MRP not recorded')).toBeInTheDocument()
    expect(screen.queryByText('₹0.00')).toBeNull()
    expect(apiFetch.mock.calls.some(([p]) => String(p) === '/pharmacy-store/stock/lots?drug_id=MED-PARA&store_id=3')).toBe(true)
  })

  it('reads an unset reorder level in words, never 0', async () => {
    routeApi()
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Reorder' }))
    const row = (await screen.findByText('Atenolol 50mg Oral Tablet')).closest('tr')!
    expect(within(row).getByText('No reorder level set')).toBeInTheDocument()
    const level = within(row).getAllByRole('cell')[2]
    expect(level.textContent).not.toMatch(/\b0\b/)
  })

  it('values near expiry at cost and MRP with the server labels', async () => {
    routeApi()
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Near expiry' }))
    expect(await screen.findByText('Expiring within 90 days')).toBeInTheDocument()
    expect(screen.getByText('₹1,000.00')).toBeInTheDocument()
    expect(screen.getByText('₹500.00')).toBeInTheDocument()
    expect(screen.getByText('Rate not configured')).toBeInTheDocument()
    expect(screen.getByText(/1 lots with no recorded cost/)).toBeInTheDocument()
  })

  it('pages the ledger back by seq and hides a patient order', async () => {
    routeApi()
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Ledger' }))
    expect(await screen.findByText("A patient's medicine order")).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show older movements' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Show older movements' })).toBeNull())
    const paths = apiFetch.mock.calls.map(([p]) => String(p)).filter(p => p.startsWith('/pharmacy-store/ledger'))
    expect(paths).toEqual(['/pharmacy-store/ledger?limit=50', '/pharmacy-store/ledger?before_seq=2&limit=50'])
    expect(screen.getAllByText('Adjustment')).toHaveLength(2)
  })

  it('hands a switched-off module back to the panel', async () => {
    apiFetch.mockImplementation(async () => {
      throw Object.assign(new Error('switched off'), {
        status: 404, detail: { error: 'module_disabled', message: 'switched off' } })
    })
    const { onModuleOff } = renderView()
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})
