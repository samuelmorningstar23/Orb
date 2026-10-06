/**
 * Pharmacy store, Purchasing (grn-7): what the screen must get right.
 *
 *  1. A draft offers 'Send for approval' to the write tier only.
 *  2. The person who raised an order is never offered Approve; a different
 *     approver is, and the approval goes to the server.
 *  3. The order form offers only what the server says is orderable, says why
 *     the rest is not, and sends whole packs and rates in paise.
 *  4. A server refusal is shown in its own words.
 *  5. Reorder suggestions start a draft with the suggested packs and the last
 *     rate paid, saved through the suggestions route.
 *  6. Cancelling needs a reason.
 *  7. A 'module_disabled' answer hands back to the panel.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PurchasingView, { viewId, viewLabel, viewOrder, viewPermission } from './PurchasingView'

const apiFetch = vi.fn()
let role = 'pharmacist'
let userId = 'PHA1'

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: userId, name: 'Test user', role }),
}))

function order(overrides: Record<string, unknown> = {}) {
  return {
    id: 11, po_number: null, status: 'draft', domain: 'pharmacy', vendor_code: 'LIFE',
    vendor_name: 'Lifeline Distributors', store_code: 'DISP', store_name: 'Main pharmacy',
    order_date: '2026-10-04', expected_date: null, is_interstate: 0, subtotal_paise: 45000,
    discount_paise: 0, taxable_value_paise: 45000, tax_paise: 5400, other_charges_paise: 0,
    rounding_adjustment_paise: 0, total_paise: 50400, created_by: 'PHA1',
    created_by_name: 'Pharmacist One', approved_by: null, remarks: null,
    number_series_configured: false,
    vendor_licence: { status: 'valid', valid_to: '2027-03-31', numbers: 'KA-1', ndps_number: null,
      warning: null },
    lines: [{
      id: 1, line_no: 1, item_code: 'MED-PARA', description: 'Paracetamol 650mg Oral Tablet',
      helix_medication_id: 'MED-PARA', uom: 'strip', quantity_milli: 10000,
      received_quantity_milli: 0, quantity_packs: 10, received_packs: 0, units_per_pack: 15,
      quantity_units: 150, unit_rate_paise: 4500, gross_paise: 45000, discount_paise: 0,
      gst_rate_bp: 1200, taxable_value_paise: 45000, cgst_paise: 2700, sgst_paise: 2700,
      igst_paise: 0, tax_paise: 5400, total_paise: 50400, status: 'open', remarks: null,
    }],
    ...overrides,
  }
}

const OPTIONS = {
  vendors: [
    { code: 'LIFE', name: 'Lifeline Distributors', state_code: '29', licence_status: 'valid',
      drug_licence_valid_to: '2027-03-31', has_ndps_licence: false, orderable: true },
    { code: 'OLD', name: 'Old Pharma', state_code: '29', licence_status: 'expired',
      drug_licence_valid_to: '2026-01-31', has_ndps_licence: false, orderable: false },
  ],
  stores: [{ code: 'DISP', name: 'Main pharmacy', pharmacy_kind: 'dispensing',
    kind_label: 'Dispensing pharmacy' }],
  drugs: [
    { id: 'MED-PARA', name: 'Paracetamol 650mg Oral Tablet', generic_name: 'Paracetamol',
      form: 'Tablet', strength: '650mg', pack_size_units: 15, purchase_uom: 'strip',
      gst_rate_bp: 1200, ndps: false, orderable: true, problems: [] },
    { id: 'MED-NEW', name: 'Newdrug 5mg Tablet', generic_name: 'Newdrug', form: 'Tablet',
      strength: '5mg', pack_size_units: null, purchase_uom: null, gst_rate_bp: null, ndps: false,
      orderable: false, problems: [{ code: 'drug_unclassified',
        message: 'Newdrug 5mg Tablet is not classified' }] },
  ],
  number_series_configured: false,
  licence_warn_days: 30,
}

const SUGGESTION = {
  store_id: 1, store_code: 'DISP', store_label: 'Main pharmacy', drug_id: 'MED-PARA',
  drug_name: 'Paracetamol 650mg Oral Tablet', form: 'Tablet', strength: '650mg',
  pack_size_units: 15, purchase_uom: 'strip', basis: 'drug_default',
  basis_label: 'Drug reorder level', reorder_level_units: 100, level_label: 'Reorder at 100',
  stock_units: 40, in_transit_units: 0, on_order_units: 0, position_units: 40,
  needs_reorder: true, suggested_units: 150, suggested_packs: 10, note: null,
  last_rate_paise: 4500, last_vendor_code: 'LIFE', last_ordered_on: '2026-09-01',
  orderable: true, problems: [],
}

function routeApi(overrides: Record<string, (path: string, init?: RequestInit) => unknown> = {}) {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path, init)
    }
    if (path.startsWith('/pharmacy-store/purchase-orders/options')) return OPTIONS
    if (path.startsWith('/pharmacy-store/purchase-orders/reorder-suggestions')) {
      return { suggestions: [SUGGESTION] }
    }
    if (path.startsWith('/pharmacy-store/purchase-orders/11')) return { purchase_order: order() }
    if (path.startsWith('/pharmacy-store/purchase-orders')) return { purchase_orders: [order()] }
    throw new Error(`unexpected ${path}`)
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([path, init]) =>
    String(path).startsWith(prefix) && (init as RequestInit | undefined)?.method === 'POST')
}

function renderView() {
  const onModuleOff = vi.fn()
  render(<PurchasingView meta={{}} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

async function openFirstOrder() {
  fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
  await screen.findByText('Order not numbered yet')
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'pharmacist'
  userId = 'PHA1'
})

afterEach(() => { vi.restoreAllMocks() })

describe('PurchasingView', () => {
  it('declares its tab', () => {
    expect([viewId, viewLabel, viewOrder, viewPermission])
      .toEqual(['purchasing', 'Purchasing', 20, 'pharmacy_store.read'])
  })

  it('offers the write tier Send for approval on a draft, and never Approve', async () => {
    routeApi()
    renderView()
    await openFirstOrder()
    expect(screen.getByRole('button', { name: 'Send for approval' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.getByText('150')).toBeInTheDocument()
    expect(screen.getByText('15 per strip')).toBeInTheDocument()
  })

  it('never offers Approve to the person who raised the order', async () => {
    role = 'admin'
    userId = 'PHA1'
    routeApi({ '/pharmacy-store/purchase-orders/11': () => ({
      purchase_order: order({ status: 'pending_approval' }) }) })
    renderView()
    await openFirstOrder()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.getByText('You raised this order, so someone else approves it.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New order' })).toBeNull()
  })

  it('lets a different approver approve, through the server', async () => {
    role = 'admin'
    userId = 'ADM1'
    routeApi({ '/pharmacy-store/purchase-orders/11': (path) => (path.endsWith('/approve')
      ? { purchase_order: order({ status: 'approved', approved_by: 'ADM1' }), warnings: [] }
      : { purchase_order: order({ status: 'pending_approval' }) }) })
    renderView()
    await openFirstOrder()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Order approved.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/purchase-orders/11/approve')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Send to vendor' })).toBeInTheDocument()
  })

  it('offers only orderable drugs and vendors and sends packs and paise', async () => {
    routeApi({ '/pharmacy-store/purchase-orders': (path, init) => {
      if (path.includes('/options')) return OPTIONS
      return init?.method === 'POST'
        ? { purchase_order: order(), warnings: [{ code: 'vendor_licence_expiring',
            message: 'Licence ends soon.' }] }
        : { purchase_orders: [order()] }
    } })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'New order' }))
    const vendor = await screen.findByLabelText('Vendor')
    const old = within(vendor).getByRole('option', { name: /Old Pharma/ }) as HTMLOptionElement
    expect(old.disabled).toBe(true)
    const drug = screen.getByLabelText('Drug, line 1')
    const blocked = within(drug).getByRole('option', { name: /Newdrug/ }) as HTMLOptionElement
    expect(blocked.disabled).toBe(true)
    expect(blocked.textContent).toContain('is not classified')

    fireEvent.change(vendor, { target: { value: 'LIFE' } })
    fireEvent.change(screen.getByLabelText('Deliver to'), { target: { value: 'DISP' } })
    fireEvent.change(drug, { target: { value: 'MED-PARA' } })
    fireEvent.change(screen.getByLabelText('Packs'), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText('Rate per pack, rupees'), { target: { value: '45.50' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText('Draft saved. Licence ends soon.')).toBeInTheDocument()
    const [, init] = posts('/pharmacy-store/purchase-orders')[0]
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      vendor_code: 'LIFE', store_code: 'DISP', expected_date: null, is_interstate: false,
      remarks: null,
      lines: [{ helix_medication_id: 'MED-PARA', packs: 10, unit_rate_paise: 4550 }],
    })
  })

  it('checks the form before sending and shows a server refusal in its words', async () => {
    routeApi({ '/pharmacy-store/purchase-orders': (path, init) => {
      if (path.includes('/options')) return OPTIONS
      if (init?.method === 'POST') {
        throw Object.assign(new Error('Lifeline Distributors has no drug licence on file.'), {
          status: 422, detail: { error: 'vendor_licence_missing',
            message: 'Lifeline Distributors has no drug licence on file.' } })
      }
      return { purchase_orders: [] }
    } })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'New order' }))
    await screen.findByLabelText('Vendor')
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText('Choose the vendor.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/purchase-orders')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Vendor'), { target: { value: 'LIFE' } })
    fireEvent.change(screen.getByLabelText('Deliver to'), { target: { value: 'DISP' } })
    fireEvent.change(screen.getByLabelText('Drug, line 1'), { target: { value: 'MED-PARA' } })
    fireEvent.change(screen.getByLabelText('Packs'), { target: { value: '1.5' } })
    fireEvent.change(screen.getByLabelText('Rate per pack, rupees'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText('Line 1: packs is a whole number, at least 1.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Packs'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText('Lifeline Distributors has no drug licence on file.'))
      .toBeInTheDocument()
  })

  it('starts a draft from reorder suggestions and saves it through that route', async () => {
    routeApi({ '/pharmacy-store/purchase-orders/from-suggestions': () => ({
      purchase_order: order(), warnings: [] }) })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Reorder suggestions' }))
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Order Paracetamol 650mg Oral Tablet' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start an order from these' }))
    expect(await screen.findByText(/Started from reorder suggestions/)).toBeInTheDocument()
    await screen.findByLabelText('Vendor')
    expect((screen.getByLabelText('Packs') as HTMLInputElement).value).toBe('10')
    expect((screen.getByLabelText('Rate per pack, rupees') as HTMLInputElement).value).toBe('45.00')
    expect((screen.getByLabelText('Deliver to') as HTMLSelectElement).value).toBe('DISP')
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved.')
    const [, init] = posts('/pharmacy-store/purchase-orders/from-suggestions')[0]
    expect(JSON.parse(String((init as RequestInit).body)).items).toEqual([
      { helix_medication_id: 'MED-PARA', packs: 10, unit_rate_paise: 4500 }])
  })

  it('needs a reason to cancel', async () => {
    routeApi({ '/pharmacy-store/purchase-orders/11': (path) => (path.endsWith('/cancel')
      ? { purchase_order: order({ status: 'cancelled', cancel_reason: 'Wrong drug' }), warnings: [] }
      : { purchase_order: order() }) })
    renderView()
    await openFirstOrder()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel order' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the order' }))
    expect(await screen.findByText('Say why the order is being cancelled.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/purchase-orders/11/cancel')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Why is it being cancelled?'), { target: { value: 'Wrong drug' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the order' }))
    expect(await screen.findByText('Order cancelled.')).toBeInTheDocument()
    const [, init] = posts('/pharmacy-store/purchase-orders/11/cancel')[0]
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ reason: 'Wrong drug' })
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
