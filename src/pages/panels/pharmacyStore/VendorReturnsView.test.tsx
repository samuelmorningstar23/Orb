/**
 * Pharmacy store, Vendor returns (grn-22): what the screen must get right.
 *
 *  1. The view registers as 'vendor-returns', order 85, behind
 *     pharmacy_store.read.
 *  2. A draft is sent back by the approve tier only, never by the person who
 *     drafted it; an ordinary return posts at once with no witness.
 *  3. A controlled return asks for a witness and their PIN before posting, and
 *     never offers the drafter as witness.
 *  4. The write tier cancels a draft only with a reason; a posted return takes
 *     the vendor's credit note number.
 *  5. The new return form lists only what the store may send back, refuses
 *     more units than a lot holds before any request, and posts whole units
 *     per lot with a request key.
 *  6. A server refusal is shown in its own words.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import VendorReturnsView, { viewId, viewLabel, viewOrder, viewPermission } from './VendorReturnsView'

const apiFetch = vi.fn()
let role = 'admin'
let userId = 'ADM1'

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: userId, name: 'Test user', role }),
}))

function line(overrides: Record<string, unknown> = {}) {
  return {
    id: 201, line_no: 1, lot_id: 'LOT-9', helix_medication_id: 'MED-AMOX',
    drug_name: 'Amoxicillin 500mg Capsule', strength: '500mg', form: 'Capsule',
    batch_no: 'AX-4', expiry_date: '2026-11-30', quantity_units: 20, unit_cost_paise: 450,
    value_paise: 9000, reason: 'Near expiry', ndps: false, lot_status: 'available',
    lot_units: 40, ...overrides,
  }
}

function doc(overrides: Record<string, unknown> = {}) {
  return {
    id: 9, return_number: null, vendor_id: 4, vendor_code: 'MEDSUP', vendor_name: 'Med Supplies',
    store_id: 2, store_code: 'SUB1', store_name: 'Store room', store_kind: 'sub_store',
    source_receipt_ref: null, status: 'draft', status_label: 'Draft', reason: 'Near expiry',
    vendor_credit_note_ref: null, total_paise: 9000, created_by: 'PHA1',
    created_by_name: 'Pharmacist One', created_at: '2026-10-09 06:00:00', posted_by: null,
    posted_by_name: null, posted_at: null, credited_at: null, cancelled_by: null,
    cancelled_by_name: null, cancelled_at: null, cancel_reason: null, remarks: null,
    number_series_configured: false, line_count: 1, total_units: 20, controlled: false,
    witness_required: false, lines: [line()], ...overrides,
  }
}

const OPTIONS = {
  stores: [{ id: 2, code: 'SUB1', name: 'Store room', pharmacy_kind: 'sub_store' }],
  vendors: [{ code: 'MEDSUP', name: 'Med Supplies', is_active: true, licence_status: 'valid',
    has_ndps_licence: true }],
  witnesses: [{ id: 'PHA1', name: 'Pharmacist One' }, { id: 'PHA3', name: 'Pharmacist Three' }],
  may_ndps: false, near_expiry_days: 90, number_series_configured: false, max_lines: 200,
}

const STOCK = {
  store: { id: 2, code: 'SUB1', name: 'Store room', pharmacy_kind: 'sub_store' },
  today: '2026-10-09', near_expiry_days: 90,
  lots: [
    { lot_id: 'LOT-9', drug_id: 'MED-AMOX', drug_name: 'Amoxicillin 500mg Capsule',
      strength: '500mg', form: 'Capsule', dispense_unit: null, ndps: false, batch_no: 'AX-4',
      expiry_date: '2026-11-30', status: 'available', quantity_units: 40, unit_cost_paise: 450,
      vendor_id: 4, vendor_code: 'MEDSUP', vendor_name: 'Med Supplies', why: 'near_expiry',
      why_label: 'Near expiry', days_left: 52 },
  ],
}

let current: ReturnType<typeof doc> = doc()

function routeApi(overrides: Record<string, (path: string, init?: RequestInit) => unknown> = {}) {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path, init)
    }
    if (path.startsWith('/pharmacy-store/vendor-returns/options')) return OPTIONS
    if (path.startsWith('/pharmacy-store/vendor-returns/stock')) return STOCK
    if (path.startsWith('/pharmacy-store/vendor-returns/9') && init?.method === 'POST') {
      return { vendor_return: current, warnings: [], replayed: false }
    }
    if (path.startsWith('/pharmacy-store/vendor-returns/9')) return { vendor_return: current }
    if (path.startsWith('/pharmacy-store/vendor-returns') && init?.method === 'POST') {
      return { vendor_return: current, warnings: [], replayed: false }
    }
    if (path.startsWith('/pharmacy-store/vendor-returns')) {
      const { lines: _lines, ...row } = current
      return { vendor_returns: [row] }
    }
    throw new Error(`unexpected ${path}`)
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([path, init]) =>
    String(path).startsWith(prefix) && (init as RequestInit | undefined)?.method === 'POST')
}

function body(call: unknown[]) {
  return JSON.parse(String((call[1] as RequestInit).body))
}

function renderView() {
  const onModuleOff = vi.fn()
  render(<VendorReturnsView meta={{}} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

async function openFirst() {
  fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
  await screen.findByText('Return not numbered yet')
}

beforeEach(() => {
  role = 'admin'
  userId = 'ADM1'
  current = doc()
  apiFetch.mockReset()
  routeApi()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('VendorReturnsView', () => {
  it('registers as the vendor returns view behind the read tier', () => {
    expect(viewId).toBe('vendor-returns')
    expect(viewLabel).toBe('Vendor returns')
    expect(viewOrder).toBe(85)
    expect(viewPermission).toBe('pharmacy_store.read')
  })

  it('lets the approve tier send an ordinary draft back with no witness', async () => {
    renderView()
    await openFirst()
    expect(screen.getByText('AX-4')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New return' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel return' })).toBeNull()
    current = doc({ status: 'posted', status_label: 'Sent back', return_number: 'PHVR-000001',
      posted_by: 'ADM1', posted_by_name: 'Test user', posted_at: '2026-10-09 06:30:00' })
    fireEvent.click(screen.getByRole('button', { name: 'Send back' }))
    expect(await screen.findByText('Sent back. The units have left the shelf.')).toBeInTheDocument()
    const sent = posts('/pharmacy-store/vendor-returns/9/post')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toEqual({})
    expect(screen.getByRole('button', { name: 'Record credit note' })).toBeInTheDocument()
  })

  it('never offers the drafter Send back', async () => {
    role = 'pharmacist'
    userId = 'PHA1'
    renderView()
    await openFirst()
    expect(screen.queryByRole('button', { name: 'Send back' })).toBeNull()
    expect(screen.getByText('You drafted this return, so somebody else with approval rights sends it back.'))
      .toBeInTheDocument()
  })

  it('asks for a witness who is not the drafter before sending a controlled return', async () => {
    current = doc({ controlled: true, witness_required: true,
      lines: [line({ drug_name: 'Morphine 10mg Injection', ndps: true, reason: 'Held off the shelf' })] })
    renderView()
    await openFirst()
    expect(screen.getByText(/Controlled stock\. Sending it back needs a second pharmacist/))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Send back' }))
    const picker = screen.getByLabelText('Witness') as HTMLSelectElement
    const choices = Array.from(picker.options).map(o => o.value)
    expect(choices).toEqual(['', 'PHA3'])
    const form = picker.closest('form') as HTMLFormElement
    fireEvent.click(within(form).getByRole('button', { name: 'Send back' }))
    expect(await screen.findByText('Choose the pharmacist who witnesses this.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/vendor-returns/9/post')).toHaveLength(0)
    fireEvent.change(picker, { target: { value: 'PHA3' } })
    fireEvent.change(screen.getByLabelText('Witness signing PIN'), { target: { value: '6284' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Send back' }))
    await waitFor(() => expect(posts('/pharmacy-store/vendor-returns/9/post')).toHaveLength(1))
    expect(body(posts('/pharmacy-store/vendor-returns/9/post')[0])).toEqual({
      witness_id: 'PHA3', witness_pin: '6284' })
  })

  it('cancels a draft only with a reason', async () => {
    role = 'pharmacist'
    userId = 'PHA2'
    renderView()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel return' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the return' }))
    expect(await screen.findByText('Say why the return is being cancelled.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/vendor-returns/9/cancel')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Why is it being cancelled?'),
      { target: { value: 'Vendor will collect next week' } })
    current = doc({ status: 'cancelled', status_label: 'Cancelled',
      cancel_reason: 'Vendor will collect next week' })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the return' }))
    expect(await screen.findByText('Return cancelled.')).toBeInTheDocument()
    expect(body(posts('/pharmacy-store/vendor-returns/9/cancel')[0])).toEqual({
      reason: 'Vendor will collect next week' })
  })

  it('records the credit note on a return that was sent back', async () => {
    current = doc({ status: 'posted', status_label: 'Sent back', return_number: 'PHVR-000001' })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Record credit note' }))
    fireEvent.click(screen.getByRole('button', { name: 'Record' }))
    expect(await screen.findByText('Enter the credit note number from the vendor.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Credit note number'), { target: { value: 'CN-118' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record' }))
    expect(await screen.findByText('Credit note recorded.')).toBeInTheDocument()
    expect(body(posts('/pharmacy-store/vendor-returns/9/credit')[0])).toEqual({
      credit_note_ref: 'CN-118' })
  })

  it('drafts a return from the returnable stock with whole units and a request key', async () => {
    role = 'pharmacist'
    userId = 'PHA2'
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'New return' }))
    await waitFor(() => expect(screen.getByLabelText('Store')).toBeInTheDocument())
    await screen.findByRole('option', { name: 'Store room' })
    fireEvent.change(screen.getByLabelText('Store'), { target: { value: 'SUB1' } })
    fireEvent.change(screen.getByLabelText('Vendor'), { target: { value: 'MEDSUP' } })
    const tick = await screen.findByLabelText(/Amoxicillin 500mg Capsule, batch AX-4: 40 units\. Near expiry/)
    fireEvent.click(tick)
    fireEvent.change(screen.getByLabelText('Units to send back'), { target: { value: '41' } })
    fireEvent.change(screen.getByLabelText('Why does it go back?'), { target: { value: 'Near expiry' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText('Amoxicillin 500mg Capsule, batch AX-4: the lot holds 40 units.'))
      .toBeInTheDocument()
    expect(posts('/pharmacy-store/vendor-returns')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Units to send back'), { target: { value: '20' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(posts('/pharmacy-store/vendor-returns')).toHaveLength(1))
    const sent = body(posts('/pharmacy-store/vendor-returns')[0])
    expect(sent).toMatchObject({ store_code: 'SUB1', vendor_code: 'MEDSUP', reason: 'Near expiry',
      lines: [{ lot_id: 'LOT-9', quantity_units: 20 }] })
    expect(String(sent.idempotency_key)).toMatch(/^vendor-return-/)
    expect(await screen.findByText(/Draft saved\. Somebody with approval rights sends it back\./))
      .toBeInTheDocument()
  })

  it('shows a refusal in its own words', async () => {
    routeApi({
      '/pharmacy-store/vendor-returns/9/post': () => {
        throw Object.assign(new Error('line 1: the lot holds 3 units, so 20 cannot go back'), {
          status: 409, detail: { error: 'exceeds_lot', message: 'x' } })
      },
    })
    renderView()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Send back' }))
    expect(await screen.findByText('Line 1: the lot holds 3 units, so 20 cannot go back'))
      .toBeInTheDocument()
  })
})
