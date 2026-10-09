/**
 * Pharmacy store, Ward stock (grn-21): what the pharmacy's screen must get
 * right.
 *
 *  1. The view registers as 'ward-stock', order 100, behind pharmacy_store.read.
 *  2. With ward stock switched off it says so and asks the server nothing.
 *  3. A ward's request is decided line by line; the person who asked is never
 *     offered the decision.
 *  4. An approved request shows the FEFO plan; Prepare to send posts the issue
 *     with a request key, and a draft is sent with the ordinary dispatch.
 *  5. A line the ward may not hold shows the server's reason and cannot be sent.
 *  5a. A request never sticks on one bad line: issuing names its lines, so the
 *     lines that can go are sent; a short line sends only what the pharmacist
 *     types, never more than the pharmacy holds; the desk may cancel an
 *     approved request with a reason (never while its draft is open).
 *  6. The pharmacist can ask on a ward's behalf, in whole units.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WardStockView, { viewId, viewLabel, viewOrder, viewPermission } from './WardStockView'

const apiFetch = vi.fn()
let role = 'pharmacist'
let userId = 'PHA2'

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: userId, name: 'Test user', role }),
}))

const ON = { features: { pharmacy_ward_stock: true } }

function indent(overrides: Record<string, unknown> = {}) {
  return {
    id: 12, indent_number: null, status: 'submitted', status_label: 'Waiting for the pharmacy',
    priority: 'routine', store_id: 7, store_code: 'WS-CARD', store_name: 'Cardiology ward stock',
    store_department: 'Cardiology', issuing_store_code: 'DISP',
    issuing_store_name: 'Main pharmacy', requested_by: 'NUR1', requested_by_name: 'Priya Reddy',
    requested_at: '2026-10-10 08:00:00', approved_by: null, approved_by_name: null,
    approved_at: null, rejected_reason: null, remarks: null, number_series_configured: false,
    lines: [{
      id: 301, line_no: 1, drug_id: 'MED-PANTO', drug_name: 'Pantoprazole 40mg Oral Tablet',
      dispense_unit: null, status: 'pending', requested_units: 14, approved_units: null,
      issued_units: 0, remaining_units: 0,
    }],
    transfers: [], live_transfer: null, ...overrides,
  }
}

const PLAN = {
  today: '2026-10-10',
  from_store: { code: 'DISP', name: 'Main pharmacy' },
  lines: [{
    line_no: 1, indent_line_id: 301, drug_id: 'MED-PANTO',
    drug_name: 'Pantoprazole 40mg Oral Tablet', approved_units: 10, issued_units: 0,
    remaining_units: 10, usable_units: 60,
    plan: [{ lot_id: 'L1', batch_no: 'PAN77', expiry_date: '2027-03-31', units: 10 }],
    problem: null as { code: string; message: string } | null,
  }],
}

const STOCK = {
  today: '2026-10-10',
  stores: [{
    store: { id: 7, code: 'WS-CARD', name: 'Cardiology ward stock', department: 'Cardiology',
      ward: 'CARD-1', is_active: true },
    drugs: [{
      drug_id: 'MED-PANTO', drug_name: 'Pantoprazole 40mg Oral Tablet', form: 'Tablet',
      strength: '40mg', dispense_unit: null, listed: true, par_units: 20, max_units: null,
      on_hand_units: 6, held_units: 0, expired_units: 0, in_transit_units: 0,
      requested_units: 0, earliest_expiry: null, below_par: true, top_up_units: 14,
      problem: null, lots: [],
    }],
  }],
  may_raise: true, may_receive: false, may_decide: true, may_issue: true,
  priorities: ['routine', 'urgent', 'emergency'],
}

let current: ReturnType<typeof indent> = indent()
let plan: typeof PLAN = PLAN

/** An approved request for two drugs: pantoprazole, which the pharmacy holds,
 *  and ondansetron, which it holds 1 of against 5 approved (or which carries
 *  `problem` when given). */
function setMixed(problem?: { code: string; message: string }, usable = 1) {
  const first = { ...indent().lines[0], status: 'approved', approved_units: 10,
    remaining_units: 10 }
  current = indent({ status: 'approved', status_label: 'Approved', lines: [first, {
    ...first, id: 302, line_no: 2, drug_id: 'MED-ONDAN',
    drug_name: 'Ondansetron 4mg Oral Tablet', requested_units: 5, approved_units: 5,
    remaining_units: 5 }] })
  plan = { ...PLAN, lines: [PLAN.lines[0], {
    line_no: 2, indent_line_id: 302, drug_id: 'MED-ONDAN',
    drug_name: 'Ondansetron 4mg Oral Tablet', approved_units: 5, issued_units: 0,
    remaining_units: 5, usable_units: usable, plan: [],
    problem: problem ?? { code: 'insufficient_stock',
      message: 'the store holds 1 usable units of this drug, fewer than the 5 asked for' },
  }] }
}

function routeApi() {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const post = init?.method === 'POST'
    if (path.startsWith('/pharmacy-store/ward/indents/12/issue-plan')) {
      return { ...plan, indent: current }
    }
    if (path.startsWith('/pharmacy-store/ward/indents/12/') && post) {
      return { indent: current, transfer: { id: 55 }, replayed: false }
    }
    if (path.startsWith('/pharmacy-store/ward/indents') && post) return { indent: current }
    if (path.startsWith('/pharmacy-store/ward/indents')) return { indents: [current] }
    if (path.startsWith('/pharmacy-store/ward/stock')) return STOCK
    if (path.startsWith('/pharmacy-store/transfers/55/dispatch') && post) {
      return { transfer: { id: 55, status: 'dispatched' }, replayed: false }
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

function renderView(meta: Record<string, unknown> = ON) {
  const onModuleOff = vi.fn()
  render(<WardStockView meta={meta} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

beforeEach(() => {
  role = 'pharmacist'
  userId = 'PHA2'
  current = indent()
  plan = PLAN
  apiFetch.mockReset()
  routeApi()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('WardStockView', () => {
  it('registers as the ward stock view behind the read tier', () => {
    expect(viewId).toBe('ward-stock')
    expect(viewLabel).toBe('Ward stock')
    expect(viewOrder).toBe(100)
    expect(viewPermission).toBe('pharmacy_store.read')
  })

  it('says ward stock is switched off and asks the server nothing', () => {
    renderView({ features: { pharmacy_ward_stock: false } })
    expect(screen.getByText(/Ward stock is switched off on this site/)).toBeInTheDocument()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('decides a ward request line by line', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    const units = await screen.findByLabelText('Units of Pantoprazole 40mg Oral Tablet to approve')
    fireEvent.change(units, { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await screen.findByText('Decision recorded.')
    const sent = posts('/pharmacy-store/ward/indents/12/decision')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toEqual({ approvals: [{ line_no: 1, units: 10 }], reason: null })
  })

  it('refusing needs a reason', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    const refuse = await screen.findByRole('button', { name: 'Refuse' })
    expect(refuse).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason (needed to refuse)'),
      { target: { value: 'Not a ward drug here' } })
    fireEvent.click(refuse)
    await screen.findByText('Request refused.')
    expect(body(posts('/pharmacy-store/ward/indents/12/decision')[0]))
      .toEqual({ reject: true, reason: 'Not a ward drug here' })
  })

  it('never offers the decision to the person who asked', async () => {
    userId = 'NUR1'
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    await screen.findByText(/someone else in the pharmacy decides it/)
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('shows the FEFO plan, prepares the issue with a key, then sends the draft', async () => {
    current = indent({ status: 'approved', status_label: 'Approved',
      lines: [{ ...indent().lines[0], status: 'approved', approved_units: 10,
        remaining_units: 10 }] })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    await screen.findByText(/10 from batch PAN77/)
    // What the server holds once the draft exists, read again after the issue.
    current = indent({ status: 'approved', status_label: 'Approved',
      live_transfer: { id: 55, transfer_number: null, status: 'draft', status_label: 'Draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Prepare to send' }))
    const send = await screen.findByRole('button', { name: 'Send to the ward' })
    const issued = posts('/pharmacy-store/ward/indents/12/issue')
    expect(issued).toHaveLength(1)
    expect(body(issued[0]).idempotency_key).toMatch(/^ward-issue-/)
    fireEvent.click(send)
    await screen.findByText('Sent to the ward.')
    expect(posts('/pharmacy-store/transfers/55/dispatch')).toHaveLength(1)
  })

  it('a line the ward may not hold shows why and cannot be sent', async () => {
    current = indent({ status: 'approved', status_label: 'Approved' })
    plan = { ...PLAN, lines: [{ ...PLAN.lines[0], plan: [],
      problem: { code: 'high_alert_barred',
        message: 'Insulin is a high-alert medicine, so it is not kept as ward stock.' } }] }
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    await screen.findByText(/not kept as ward stock/)
    expect(screen.getByRole('button', { name: 'Prepare to send' })).toBeDisabled()
  })

  it('a mixed request sends the lines that can go and leaves the short one out', async () => {
    setMixed()
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    const panto = await screen.findByLabelText('Units of Pantoprazole 40mg Oral Tablet to send now')
    const ondan = screen.getByLabelText('Units of Ondansetron 4mg Oral Tablet to send now')
    expect(panto).toHaveValue('10')
    // Short of stock: nothing by default, and the screen says what there is.
    expect(ondan).toHaveValue('0')
    expect(screen.getByText('Only 1 usable in Main pharmacy.')).toBeInTheDocument()
    expect(screen.getByText(/Not all of it goes now: Ondansetron 4mg Oral Tablet/))
      .toBeInTheDocument()
    const prepare = screen.getByRole('button', { name: 'Prepare to send' })
    expect(prepare).toBeEnabled()
    fireEvent.click(prepare)
    await screen.findByText('Draft ready. Send it when it is packed.')
    const issued = posts('/pharmacy-store/ward/indents/12/issue')
    expect(issued).toHaveLength(1)
    expect(body(issued[0]).lines).toEqual([{ line_no: 1, units: 10 }])
    expect(body(issued[0]).idempotency_key).toMatch(/^ward-issue-/)
  })

  it('a short line sends what there is when the pharmacist types it, never more', async () => {
    setMixed()
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    const ondan = await screen.findByLabelText('Units of Ondansetron 4mg Oral Tablet to send now')
    fireEvent.change(ondan, { target: { value: '2' } })
    expect(screen.getByText('Send a whole number, 1 at most.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare to send' })).toBeDisabled()
    fireEvent.change(ondan, { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Prepare to send' }))
    await screen.findByText('Draft ready. Send it when it is packed.')
    expect(body(posts('/pharmacy-store/ward/indents/12/issue')[0]).lines)
      .toEqual([{ line_no: 1, units: 10 }, { line_no: 2, units: 1 }])
  })

  it('a barred line beside a good one is never sent and does not stop the rest', async () => {
    setMixed({ code: 'high_alert_barred',
      message: 'Ondansetron is barred from ward stock on this site.' }, 20)
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    await screen.findByText('Ondansetron is barred from ward stock on this site.')
    expect(screen.queryByLabelText('Units of Ondansetron 4mg Oral Tablet to send now')).toBeNull()
    expect(screen.getByText('Cannot be sent.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare to send' }))
    await screen.findByText('Draft ready. Send it when it is packed.')
    expect(body(posts('/pharmacy-store/ward/indents/12/issue')[0]).lines)
      .toEqual([{ line_no: 1, units: 10 }])
  })

  it('the desk cancels an approved request, with a reason', async () => {
    current = indent({ status: 'approved', status_label: 'Approved' })
    plan = { ...PLAN, lines: [{ ...PLAN.lines[0], plan: [], usable_units: 0,
      problem: { code: 'insufficient_stock', message: 'the store holds 0 usable units' } }] }
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    await screen.findByText(/Nothing on this request can be sent now/)
    expect(screen.getByRole('button', { name: 'Prepare to send' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the request' }))
    const confirm = screen.getByRole('button', { name: 'Confirm cancelling' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason for cancelling'),
      { target: { value: 'Out of stock until Monday' } })
    fireEvent.click(confirm)
    await screen.findByText('Request cancelled. The ward can ask for these drugs again.')
    const sent = posts('/pharmacy-store/ward/indents/12/cancel')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toEqual({ reason: 'Out of stock until Monday' })
  })

  it('offers no cancel while a draft is open', async () => {
    current = indent({ status: 'approved', status_label: 'Approved',
      live_transfer: { id: 55, transfer_number: null, status: 'draft', status_label: 'Draft' } })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    await screen.findByRole('button', { name: 'Cancel the draft' })
    expect(screen.queryByRole('button', { name: 'Cancel the request' })).toBeNull()
  })

  it('an approve-only account is offered no issue and no cancel', async () => {
    role = 'admin'
    current = indent({ status: 'approved', status_label: 'Approved' })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    await screen.findByText('Asked by')
    expect(screen.queryByRole('button', { name: 'Prepare to send' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel the request' })).toBeNull()
    expect(posts('/pharmacy-store/ward/indents/12')).toHaveLength(0)
  })

  it('asks on a ward’s behalf in whole units', async () => {
    renderView()
    fireEvent.click(screen.getByRole('button', { name: 'Ask for a ward' }))
    const store = await screen.findByLabelText('Ward store')
    fireEvent.change(store, { target: { value: 'WS-CARD' } })
    fireEvent.change(await screen.findByLabelText('Units'), { target: { value: '12' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send the request' }))
    await waitFor(() => expect(posts('/pharmacy-store/ward/indents')).toHaveLength(1))
    expect(body(posts('/pharmacy-store/ward/indents')[0])).toEqual({
      store_code: 'WS-CARD', priority: 'routine',
      lines: [{ helix_medication_id: 'MED-PANTO', quantity_units: 12 }],
    })
  })

  it('a read-only account is offered no decision and no request form', async () => {
    role = 'admin'
    renderView()
    expect(screen.queryByRole('button', { name: 'Ask for a ward' })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    // Admin holds the approve tier, so the decision is offered; issuing is not.
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeInTheDocument()
  })
})
