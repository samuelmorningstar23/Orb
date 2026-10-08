/**
 * Pharmacy store, Transfers (grn-11): what the screen must get right.
 *
 *  1. The view registers as 'transfers', order 50, behind pharmacy_store.read.
 *  2. A draft offers Send, Plan again and Cancel to the write tier only; a
 *     read-only account sees the transfer and no actions and no New transfer.
 *  3. Send on an ordinary transfer posts at once with no witness.
 *  4. A controlled transfer asks for a witness and their PIN before Send and
 *     before Receive, and posts both.
 *  5. The person who sent a transfer is never offered Receive.
 *  6. A short receipt needs a reason before anything is posted; an unticked
 *     line stays on its way and is not sent.
 *  7. The new transfer form sends whole units per drug with a request key,
 *     after loading what the sending store holds.
 *  8. Cancelling needs a reason; a server refusal is shown in its own words;
 *     a 'module_disabled' answer hands back to the panel.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import TransfersView, { viewId, viewLabel, viewOrder, viewPermission } from './TransfersView'

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

function line(overrides: Record<string, unknown> = {}) {
  return {
    id: 101, line_no: 1, helix_medication_id: 'MED-PARA',
    drug_name: 'Paracetamol 650mg Oral Tablet', generic_name: 'Paracetamol', form: 'Tablet',
    strength: '650mg', dispense_unit: null, from_lot_id: 'LOT-1', to_lot_id: null,
    batch_no: 'PB-7', expiry_date: '2027-03-31', quantity_units: 10, received_units: null,
    shortfall_units: null, discrepancy_reason: null, ndps: false, ...overrides,
  }
}

function transfer(overrides: Record<string, unknown> = {}) {
  return {
    id: 7, transfer_number: null, from_store_id: 2, to_store_id: 1, from_store_code: 'SUB1',
    from_store_name: 'Store room', from_store_kind: 'sub_store', to_store_code: 'DISP',
    to_store_name: 'Main pharmacy', to_store_kind: 'dispensing', status: 'draft',
    status_label: 'Draft', created_by: 'PHA1', created_by_name: 'Pharmacist One',
    created_at: '2026-10-06 09:15:00', dispatched_by: null, dispatched_by_name: null,
    dispatched_at: null, received_by: null, received_by_name: null, received_at: null,
    cancelled_by: null, cancelled_by_name: null, cancelled_at: null, cancel_reason: null,
    remarks: null, number_series_configured: false, line_count: 1, total_units: 10,
    in_transit_units: 0, controlled: false, witness_required: false, lines: [line()],
    ...overrides,
  }
}

const OPTIONS = {
  stores: [
    { id: 1, code: 'DISP', name: 'Main pharmacy', pharmacy_kind: 'dispensing' },
    { id: 2, code: 'SUB1', name: 'Store room', pharmacy_kind: 'sub_store' },
    { id: 3, code: 'VAULT', name: 'NDPS vault', pharmacy_kind: 'ndps_vault' },
  ],
  witnesses: [{ id: 'PHA3', name: 'Pharmacist Three' }],
  may_ndps: true,
  ndps_witness_always: true,
  number_series_configured: false,
  max_drugs: 100,
}

const STOCK = {
  store: { id: 2, code: 'SUB1', name: 'Store room', pharmacy_kind: 'sub_store' },
  today: '2026-10-06',
  drugs: [
    { drug_id: 'MED-PARA', drug_name: 'Paracetamol 650mg Oral Tablet', form: 'Tablet',
      strength: '650mg', dispense_unit: null, ndps: false, classified: true, usable_units: 40,
      lot_count: 2, earliest_expiry: '2027-01-31' },
    { drug_id: 'MED-OLD', drug_name: 'Olddrug 5mg Tablet', form: 'Tablet', strength: '5mg',
      dispense_unit: null, ndps: false, classified: false, usable_units: 9, lot_count: 1,
      earliest_expiry: '2027-01-31' },
  ],
}

let current: ReturnType<typeof transfer> = transfer()

function routeApi(overrides: Record<string, (path: string, init?: RequestInit) => unknown> = {}) {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path, init)
    }
    if (path.startsWith('/pharmacy-store/transfers/options')) return OPTIONS
    if (path.startsWith('/pharmacy-store/transfers/stock')) return STOCK
    if (path.startsWith('/pharmacy-store/transfers/7') && init?.method === 'POST') {
      return { transfer: current, replayed: false }
    }
    if (path.startsWith('/pharmacy-store/transfers/7')) return { transfer: current }
    if (path.startsWith('/pharmacy-store/transfers') && init?.method === 'POST') {
      return { transfer: current, replayed: false }
    }
    if (path.startsWith('/pharmacy-store/transfers')) {
      const { lines: _lines, ...row } = current
      return { transfers: [row] }
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
  render(<TransfersView meta={{}} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

async function openFirst() {
  fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
  await screen.findByText('Transfer not numbered yet')
}

beforeEach(() => {
  role = 'pharmacist'
  userId = 'PHA2'
  current = transfer()
  apiFetch.mockReset()
  routeApi()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('TransfersView', () => {
  it('registers as the transfers view behind the read tier', () => {
    expect(viewId).toBe('transfers')
    expect(viewLabel).toBe('Transfers')
    expect(viewOrder).toBe(50)
    expect(viewPermission).toBe('pharmacy_store.read')
  })

  it('offers a draft Send, Plan again and Cancel, and Send posts with no witness', async () => {
    renderView()
    await openFirst()
    expect(screen.getByText('Batch', { selector: 'th' })).toBeInTheDocument()
    expect(screen.getByText('PB-7')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Plan again' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel transfer' })).toBeInTheDocument()
    current = transfer({ status: 'dispatched', status_label: 'On its way', dispatched_by: 'PHA2',
      dispatched_by_name: 'Test user', dispatched_at: '2026-10-06 09:20:00',
      transfer_number: 'PHTRF-2026-27-000001', in_transit_units: 10 })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByText('Sent. The units are on their way.')
    const sent = posts('/pharmacy-store/transfers/7/dispatch')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toEqual({})
    // The sender is never offered Receive.
    expect(screen.queryByRole('button', { name: 'Receive' })).toBeNull()
    expect(screen.getByText('You sent this transfer, so somebody else receives it.'))
      .toBeInTheDocument()
  })

  it('shows a read-only account the transfer with no actions and no new transfer', async () => {
    role = 'admin'
    renderView()
    await openFirst()
    expect(screen.queryByRole('button', { name: 'New transfer' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel transfer' })).toBeNull()
  })

  it('asks for a witness and PIN before sending a controlled transfer', async () => {
    current = transfer({ controlled: true, witness_required: true, from_store_code: 'VAULT',
      from_store_name: 'NDPS vault', from_store_kind: 'ndps_vault',
      lines: [line({ drug_name: 'Morphine 10mg Injection', ndps: true })] })
    renderView()
    await openFirst()
    expect(screen.getByText(/Controlled stock\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    const form = screen.getByLabelText('Witness').closest('form') as HTMLFormElement
    fireEvent.click(within(form).getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('Choose the pharmacist who witnesses this.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/transfers/7/dispatch')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Witness'), { target: { value: 'PHA3' } })
    fireEvent.change(screen.getByLabelText('Witness signing PIN'), { target: { value: '4826' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(posts('/pharmacy-store/transfers/7/dispatch')).toHaveLength(1))
    expect(body(posts('/pharmacy-store/transfers/7/dispatch')[0]))
      .toEqual({ witness_id: 'PHA3', witness_pin: '4826' })
  })

  it('needs a reason for a short line and leaves an unticked line on its way', async () => {
    current = transfer({ status: 'dispatched', status_label: 'On its way', dispatched_by: 'PHA1',
      dispatched_by_name: 'Pharmacist One', transfer_number: 'PHTRF-1', in_transit_units: 14,
      lines: [line(), line({ id: 102, line_no: 2, helix_medication_id: 'MED-AMOX',
        drug_name: 'Amoxicillin 500mg Capsule', batch_no: 'AX-2', quantity_units: 4 })] })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Receive' }))
    const list = screen.getByRole('list', { name: 'Lines to receive' })
    const boxes = within(list).getAllByRole('checkbox')
    fireEvent.click(boxes[1])
    fireEvent.change(screen.getByLabelText('Units received', { selector: '#psw-recv-units-101' }),
      { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record receipt' }))
    expect(await screen.findByText(/say what happened to the 2 units/)).toBeInTheDocument()
    expect(posts('/pharmacy-store/transfers/7/receive')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('What happened to the rest?'),
      { target: { value: 'Two strips crushed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record receipt' }))
    await waitFor(() => expect(posts('/pharmacy-store/transfers/7/receive')).toHaveLength(1))
    expect(body(posts('/pharmacy-store/transfers/7/receive')[0])).toEqual({
      lines: [{ line_id: 101, received_units: 8, discrepancy_reason: 'Two strips crushed' }],
    })
  })

  it('asks a controlled receipt for its witness', async () => {
    current = transfer({ status: 'dispatched', status_label: 'On its way', dispatched_by: 'PHA1',
      controlled: true, witness_required: true, transfer_number: 'PHTRF-2',
      lines: [line({ ndps: true, drug_name: 'Morphine 10mg Injection' })] })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Receive' }))
    fireEvent.change(screen.getByLabelText('Witness'), { target: { value: 'PHA3' } })
    fireEvent.change(screen.getByLabelText('Witness signing PIN'), { target: { value: '1111' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record receipt' }))
    await waitFor(() => expect(posts('/pharmacy-store/transfers/7/receive')).toHaveLength(1))
    expect(body(posts('/pharmacy-store/transfers/7/receive')[0])).toEqual({
      lines: [{ line_id: 101, received_units: 10, discrepancy_reason: null }],
      witness_id: 'PHA3', witness_pin: '1111',
    })
  })

  it('says when received units were held off the shelf', async () => {
    current = transfer({ status: 'received', status_label: 'Received', dispatched_by: 'PHA1',
      received_by: 'PHA2', transfer_number: 'PHTRF-3',
      lines: [
        line({ received_units: 10, shortfall_units: 0, to_lot_id: 'LOT-9',
          to_lot_status: 'available', to_lot_status_label: 'available' }),
        line({ id: 102, line_no: 2, helix_medication_id: 'MED-AMOX', batch_no: 'AX-2',
          drug_name: 'Amoxicillin 500mg Capsule', quantity_units: 4, received_units: 4,
          shortfall_units: 0, to_lot_id: 'LOT-10', to_lot_status: 'recalled',
          to_lot_status_label: 'recalled' }),
      ] })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(await screen.findByText('Held off the shelf: recalled')).toBeInTheDocument()
    expect(screen.getAllByText(/Held off the shelf/)).toHaveLength(1)
  })

  it('shows a refusal in its own words', async () => {
    routeApi({
      '/pharmacy-store/transfers/7/dispatch': () => {
        throw Object.assign(new Error('line 1 (Paracetamol 650mg Oral Tablet, batch PB-7): the lot holds 3 units, not 10. Plan the transfer again. Nothing was changed.'),
          { status: 409, detail: { error: 'plan_stale', message: 'stale' } })
      },
    })
    renderView()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText(/Plan the transfer again/)).toBeInTheDocument()
    // The server's clause is shown as a sentence.
    expect(screen.getByRole('alert').textContent).toMatch(/^Line 1 \(Paracetamol/)
  })

  it('needs a reason to cancel', async () => {
    renderView()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel transfer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the transfer' }))
    expect(await screen.findByText('Say why the transfer is being cancelled.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/transfers/7/cancel')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Why is it being cancelled?'),
      { target: { value: 'Raised against the wrong store' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the transfer' }))
    await waitFor(() => expect(posts('/pharmacy-store/transfers/7/cancel')).toHaveLength(1))
    expect(body(posts('/pharmacy-store/transfers/7/cancel')[0]))
      .toEqual({ reason: 'Raised against the wrong store' })
  })

  it('raises a draft in whole units per drug with a request key', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'New transfer' }))
    fireEvent.change(await screen.findByLabelText('From'), { target: { value: 'SUB1' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: 'DISP' } })
    const drug = await screen.findByLabelText('Drug, line 1')
    const unclassified = within(drug).getByRole('option', { name: 'Olddrug 5mg Tablet (not classified)' })
    expect(unclassified).toBeDisabled()
    fireEvent.change(drug, { target: { value: 'MED-PARA' } })
    expect(screen.getByText(/40 usable/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Units'), { target: { value: '50' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft and plan' }))
    expect(await screen.findByText('Line 1: the store holds 40 usable units.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Units'), { target: { value: '12' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft and plan' }))
    await waitFor(() => expect(posts('/pharmacy-store/transfers')
      .filter(([p]) => p === '/pharmacy-store/transfers')).toHaveLength(1))
    const sent = body(posts('/pharmacy-store/transfers').filter(([p]) => p === '/pharmacy-store/transfers')[0])
    expect(sent).toMatchObject({
      from_store_code: 'SUB1', to_store_code: 'DISP', remarks: null,
      lines: [{ helix_medication_id: 'MED-PARA', quantity_units: 12 }],
    })
    expect(typeof sent.idempotency_key).toBe('string')
    expect(sent.idempotency_key.length).toBeGreaterThan(8)
    expect(await screen.findByText('Draft saved. Pick the lots on the plan, then send it.'))
      .toBeInTheDocument()
  })

  it('hands a switched-off module back to the panel', async () => {
    routeApi({
      '/pharmacy-store/transfers': () => {
        throw Object.assign(new Error('off'), { status: 404,
          detail: { error: 'module_disabled', module: 'pharmacy-store' } })
      },
    })
    const { onModuleOff } = renderView()
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})
