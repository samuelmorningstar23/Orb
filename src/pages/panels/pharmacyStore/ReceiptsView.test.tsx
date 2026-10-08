/**
 * Pharmacy store, Receipts (grn-8): what the screen must get right.
 *
 *  1. The tab declares itself: receipts, order 30, the read tier.
 *  2. A receipt opens with its batch, expiry, packs, free packs, units and
 *     cost per unit as the server worked them out.
 *  3. Recording a delivery needs the expiry typed twice: two dates that differ
 *     are refused on the screen and nothing is sent; matching dates are sent
 *     as both fields, the MRP in paise, whole packs, and one Idempotency-Key
 *     that a retried save repeats.
 *  4. The second expiry box refuses a paste.
 *  5. Short and expired shelf life are shown against the hospital's date the
 *     server sent, never the browser's clock.
 *  6. A short-dated draft: the write tier is told an approver must accept it
 *     and is offered no post; an approver who did not receive it accepts with
 *     a reason (none is refused on the screen); the receiver is told someone
 *     else must.
 *  7. Posting an ordinary draft sends the post; a replayed post says nothing
 *     moved again. A server refusal is shown in its own words.
 *  8. Cancelling a draft needs a reason.
 *  9. The approve tier records no delivery and posts no ordinary draft.
 * 10. A 'module_disabled' answer hands back to the panel.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs, headers and bodies.
 */
import { createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { formatClinicalDateKey } from '../../../lib/datetime'
import ReceiptsView, { viewId, viewLabel, viewOrder, viewPermission } from './ReceiptsView'

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

const RECEIVABLE = {
  orders: [{
    id: 21, po_number: 'PHPO-0001', status: 'issued', vendor_code: 'LIFE',
    vendor_name: 'Lifeline Distributors', store_code: 'DISP', store_name: 'Main pharmacy',
    order_date: '2026-10-01',
    lines: [{
      line_no: 1, helix_medication_id: 'MED-PARA', description: 'Paracetamol 650mg Oral Tablet',
      uom: 'strip', units_per_pack: 15, ordered_packs: 10, received_packs: 0, outstanding_packs: 10,
      unit_rate_paise: 4500, gst_rate_bp: 1200, ndps: false,
    }],
  }],
  min_shelf_life_days: 90,
  number_series_configured: false,
  // The hospital's date, from the server. The browser's clock is never asked.
  today: '2026-10-09',
}

function receiptLine(overrides: Record<string, unknown> = {}) {
  return {
    id: 1, line_no: 1, item_code: 'MED-PARA', description: 'Paracetamol 650mg Oral Tablet',
    helix_medication_id: 'MED-PARA', uom: 'strip', units_per_pack: 15, batch_no: 'BX101',
    expiry_date: '2027-12-31', mfg_date: null, mrp_pack_paise: 6000, unit_rate_paise: 4500,
    total_paise: 30240, packs: 6, free_packs: 1, rejected_packs: 0, rejection_reason: null,
    units: 105, unit_cost_paise: 288, days_to_expiry: 448, expired: false, short_dated: false,
    rate_above_mrp: false, helix_lot_ref: null,
    ...overrides,
  }
}

function receipt(overrides: Record<string, unknown> = {}) {
  return {
    id: 31, grn_number: null, status: 'draft', purchase_order_id: 21, po_number: 'PHPO-0001',
    vendor_code: 'LIFE', vendor_name: 'Lifeline Distributors', store_code: 'DISP',
    store_name: 'Main pharmacy', vendor_invoice_number: 'INV-9', vendor_invoice_date: '2026-10-08',
    vendor_invoice_total_paise: 30240, taxable_value_paise: 27000, tax_paise: 3240,
    total_paise: 30240, received_by: 'PHA1', received_by_name: 'Pharmacist One',
    received_at: '2026-10-09 04:00:00', posted_by: null, posted_at: null, needs_approval: false,
    has_expired_line: false, number_series_configured: false, min_shelf_life_days: 90,
    warnings: [], lines: [receiptLine()],
    ...overrides,
  }
}

const SHORT = receipt({
  needs_approval: true,
  warnings: [{ code: 'short_shelf_life', line_no: 1,
    message: 'Paracetamol 650mg Oral Tablet batch BX101 has 30 days left, under the 90 the hospital asks for.' }],
  lines: [receiptLine({ expiry_date: '2026-11-08', days_to_expiry: 30, short_dated: true })],
})

type Handler = (path: string, init?: RequestInit) => unknown

function routeApi(overrides: Record<string, Handler> = {}, doc: Record<string, unknown> = receipt()) {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path, init)
    }
    if (path.startsWith('/pharmacy-store/goods-receipts/receivable')) return RECEIVABLE
    if (path.startsWith('/pharmacy-store/goods-receipts/31')) return { goods_receipt: doc }
    if (path.startsWith('/pharmacy-store/goods-receipts')) return { goods_receipts: [doc] }
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
  render(<ReceiptsView meta={{}} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

async function openFirstReceipt() {
  fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
  await screen.findByText('Receipt not numbered yet')
}

async function startDelivery() {
  fireEvent.click(await screen.findByRole('button', { name: 'Record a delivery' }))
  fireEvent.change(await screen.findByLabelText('Order'), { target: { value: '21' } })
  fireEvent.change(screen.getByLabelText('Vendor invoice number'), { target: { value: 'INV-9' } })
  fireEvent.change(screen.getByLabelText('Invoice date'), { target: { value: '2026-10-08' } })
  return within(screen.getByRole('listitem', { name: 'Paracetamol 650mg Oral Tablet' }))
}

function fillLine(line: ReturnType<typeof within>, values: Record<string, string>) {
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(line.getByLabelText(label), { target: { value } })
  }
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'pharmacist'
  userId = 'PHA1'
})

afterEach(() => { vi.restoreAllMocks() })

describe('ReceiptsView', () => {
  it('declares its tab', () => {
    expect([viewId, viewLabel, viewOrder, viewPermission])
      .toEqual(['receipts', 'Receipts', 30, 'pharmacy_store.read'])
  })

  it('opens a receipt with its batch, expiry, free packs, units and cost per unit', async () => {
    routeApi()
    renderView()
    expect(await screen.findByText('Draft, not on the shelf yet')).toBeInTheDocument()
    await openFirstReceipt()
    expect(screen.getByText('BX101')).toBeInTheDocument()
    expect(screen.getByText(formatClinicalDateKey('2027-12-31'))).toBeInTheDocument()
    expect(screen.getByText('448 days left')).toBeInTheDocument()
    expect(screen.getByText('and 1 free')).toBeInTheDocument()
    expect(screen.getByText('105')).toBeInTheDocument()
    expect(screen.getByText('₹2.88')).toBeInTheDocument()
  })

  it('refuses two different expiry dates on the screen and sends nothing', async () => {
    routeApi()
    renderView()
    const line = await startDelivery()
    fillLine(line, {
      'Packs received': '6', 'Batch number': 'BX101', 'Expiry date': '2027-12-31',
      'Expiry date, typed again': '2027-12-30', 'MRP per pack, rupees': '60',
    })
    expect(line.getByRole('alert')).toHaveTextContent('The two expiry dates do not match')
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText(/the two expiry dates do not match\. Check the pack and type/i))
      .toBeInTheDocument()
    expect(posts('/pharmacy-store/goods-receipts')).toHaveLength(0)
  })

  it('sends both expiry fields, the MRP in paise and one key that a retry repeats', async () => {
    let attempt = 0
    routeApi({
      '/pharmacy-store/goods-receipts': (path, init) => {
        if (init?.method !== 'POST') return path.includes('receivable') ? RECEIVABLE
          : path.startsWith('/pharmacy-store/goods-receipts/31') ? { goods_receipt: receipt() }
            : { goods_receipts: [receipt()] }
        attempt += 1
        if (attempt === 1) throw Object.assign(new Error('offline'), { status: 0 })
        return { goods_receipt: receipt(), warnings: [], replayed: false }
      },
    })
    renderView()
    const line = await startDelivery()
    fillLine(line, {
      'Packs received': '6', 'Free packs': '1', 'Batch number': 'BX101',
      'Expiry date': '2027-12-31', 'Expiry date, typed again': '2027-12-31',
      'MRP per pack, rupees': '60',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText(/Could not reach the pharmacy store/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText('Draft saved. Nothing is on the shelf until it is posted.'))
      .toBeInTheDocument()

    const sent = posts('/pharmacy-store/goods-receipts')
    expect(sent).toHaveLength(2)
    const keys = sent.map(c => ((c[1] as RequestInit).headers as Record<string, string>)['Idempotency-Key'])
    expect(keys[0]).toMatch(/^grn-/)
    expect(keys[1]).toBe(keys[0])
    expect(body(sent[1])).toEqual({
      purchase_order_id: 21,
      vendor_invoice_number: 'INV-9',
      vendor_invoice_date: '2026-10-08',
      vendor_invoice_total_paise: null,
      remarks: null,
      lines: [{
        po_line_no: 1, packs: 6, free_packs: 1, rejected_packs: 0, batch_no: 'BX101',
        expiry_date: '2027-12-31', expiry_date_confirm: '2027-12-31', mrp_pack_paise: 6000,
      }],
    })
  })

  it('refuses a paste into the second expiry box', async () => {
    routeApi()
    renderView()
    const line = await startDelivery()
    const again = line.getByLabelText('Expiry date, typed again')
    const paste = createEvent.paste(again)
    fireEvent(again, paste)
    expect(paste.defaultPrevented).toBe(true)
  })

  it('judges shelf life on the date the server sent', async () => {
    routeApi()
    renderView()
    const line = await startDelivery()
    fillLine(line, { 'Expiry date': '2026-11-08' })
    expect(line.getByText(/30 days of shelf life left, under the 90/)).toBeInTheDocument()
    fillLine(line, { 'Expiry date': '2026-10-08' })
    expect(line.getByText('This batch has expired. Expired stock is never received.'))
      .toBeInTheDocument()
  })

  it('tells the write tier that an approver must accept a short-dated batch', async () => {
    routeApi({}, SHORT)
    renderView()
    await openFirstReceipt()
    expect(screen.getByText(/A person with approval rights must accept it, with a reason/))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Post to stock' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Accept short expiry and post' })).toBeNull()
  })

  it('lets an approver who did not receive it accept short expiry, with a reason', async () => {
    role = 'admin'
    userId = 'ADM1'
    routeApi({
      '/pharmacy-store/goods-receipts/31/post': () => ({
        goods_receipt: { ...SHORT, status: 'posted', grn_number: 'PHGRN-0001', needs_approval: false },
        warnings: [], replayed: false,
      }),
    }, SHORT)
    renderView()
    await openFirstReceipt()
    fireEvent.click(screen.getByRole('button', { name: 'Accept short expiry and post' }))
    expect(await screen.findByText('Give the reason for accepting stock with short expiry.'))
      .toBeInTheDocument()
    expect(posts('/pharmacy-store/goods-receipts/31/post')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Why is this stock accepted?'),
      { target: { value: 'Only stock the vendor has' } })
    fireEvent.click(screen.getByRole('button', { name: 'Accept short expiry and post' }))
    await waitFor(() => expect(posts('/pharmacy-store/goods-receipts/31/post')).toHaveLength(1))
    expect(body(posts('/pharmacy-store/goods-receipts/31/post')[0]))
      .toEqual({ short_expiry_reason: 'Only stock the vendor has' })
    expect(await screen.findByText('Short expiry accepted and posted to stock.')).toBeInTheDocument()
  })

  it('tells the receiver that someone else accepts their short expiry', async () => {
    role = 'admin'
    userId = 'PHA1'
    routeApi({}, SHORT)
    renderView()
    await openFirstReceipt()
    expect(screen.getByText(/You received this delivery, so someone else/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Accept short expiry and post' })).toBeNull()
  })

  it('posts an ordinary draft and says when nothing moved again', async () => {
    routeApi({
      '/pharmacy-store/goods-receipts/31/post': () => ({
        goods_receipt: receipt({ status: 'posted', grn_number: 'PHGRN-0001' }),
        warnings: [], replayed: true,
      }),
    })
    renderView()
    await openFirstReceipt()
    fireEvent.click(screen.getByRole('button', { name: 'Post to stock' }))
    expect(await screen.findByText('This receipt was already posted. Nothing moved again.'))
      .toBeInTheDocument()
    expect(body(posts('/pharmacy-store/goods-receipts/31/post')[0])).toEqual({})
  })

  it('shows a server refusal in its own words', async () => {
    const words = 'batch BX101 of this drug is already on file with expiry 2027-06-30. Nothing was changed.'
    routeApi({
      '/pharmacy-store/goods-receipts/31/post': () => {
        throw Object.assign(new Error(words), {
          status: 409, detail: { error: 'batch_expiry_mismatch', message: words } })
      },
    })
    renderView()
    await openFirstReceipt()
    fireEvent.click(screen.getByRole('button', { name: 'Post to stock' }))
    expect(await screen.findByText(words)).toBeInTheDocument()
  })

  it('needs a reason to cancel a draft', async () => {
    routeApi({
      '/pharmacy-store/goods-receipts/31/cancel': () => ({
        goods_receipt: receipt({ status: 'cancelled', cancel_reason: 'Wrong delivery' }),
        warnings: [], replayed: false,
      }),
    })
    renderView()
    await openFirstReceipt()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel draft' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the draft' }))
    expect(await screen.findByText('Say why the draft is being cancelled.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/goods-receipts/31/cancel')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Why is the draft being cancelled?'),
      { target: { value: 'Wrong delivery' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the draft' }))
    expect(await screen.findByText('Draft cancelled.')).toBeInTheDocument()
    expect(body(posts('/pharmacy-store/goods-receipts/31/cancel')[0]))
      .toEqual({ reason: 'Wrong delivery' })
  })

  it('offers the approve tier no delivery form and no ordinary post', async () => {
    role = 'admin'
    userId = 'ADM1'
    routeApi()
    renderView()
    await openFirstReceipt()
    expect(screen.queryByRole('button', { name: 'Record a delivery' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Post to stock' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel draft' })).toBeNull()
    expect(screen.getByText('A pharmacist posts this receipt to stock.')).toBeInTheDocument()
  })

  it('hands a module_disabled answer back to the panel', async () => {
    routeApi({
      '/pharmacy-store/goods-receipts': () => {
        throw Object.assign(new Error('off'), {
          status: 404, detail: { error: 'module_disabled', module: 'pharmacy-store' } })
      },
    })
    const { onModuleOff } = renderView()
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})
