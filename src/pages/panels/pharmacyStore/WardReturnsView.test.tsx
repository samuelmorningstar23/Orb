/**
 * Pharmacy store, Ward returns (grn-15): what the pharmacist's screen must get
 * right.
 *
 *  1. The view registers as 'ward-returns', order 88, behind
 *     pharmacy_store.write (every answer names a patient).
 *  2. A return waiting for inspection is written off with a reason; a
 *     controlled one asks for a witness and their PIN first; the person who
 *     took it in is not offered the write-off.
 *  3. Restock is never offered while the site has not ruled it (GRN-RS3), and
 *     where it has, not to the person who took the return in; the server's
 *     reasons are shown when it cannot go back.
 *  4. Taking a return in at the counter posts whole units for the order and
 *     batch with a request key, and refuses more than can come back before
 *     any request.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WardReturnsView, { viewId, viewLabel, viewOrder, viewPermission } from './WardReturnsView'

const apiFetch = vi.fn()
let userId = 'PH1'

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: userId, name: 'Test pharmacist', role: 'pharmacist' }),
}))

function ret(over: Record<string, unknown> = {}) {
  return {
    id: 7, return_number: 'PHRET-0007', status: 'pending_inspection',
    status_label: 'Waiting for inspection', patient_id: 'P1', patient_name: 'Anita Rao',
    patient_mrn: 'M1', patient_bed: 'GM-4', patient_ward: 'GM', department: 'General Medicine',
    helix_order_id: 'HX-1', helix_medication_id: 'MED-1', drug_name: 'Amoxicillin',
    strength: '500 mg', form: 'capsule', dispense_unit: 'capsules', batch_no: 'B77',
    expiry_date: '2027-01-31', quantity_units: 3, seal_intact: true, controlled: false,
    cold_chain: false, original_lot_id: 'LOT-1', original_lot_status: 'available',
    quarantine_lot_id: 'LOT-Q', quarantine_units: 3, store_code: 'RQ1',
    store_name: 'Returns quarantine', returned_by: 'NUR1', returned_by_name: 'Ravi Iyer',
    received_by: 'NUR1', received_by_name: 'Ravi Iyer', occurred_at: '2026-10-09T10:00:00Z',
    created_at: '2026-10-09 10:00:00', inspected_by: null, inspected_by_name: null,
    inspected_at: null, inspection_note: null, note: 'Course stopped',
    credit: { units: 3, unit_mrp_paise: 100, amount_paise: 300, basis: 'batch_mrp',
      helix_order_id: 'HX-1', patient_id: 'P1' },
    restock: { allowed: false, code: 'restock_not_allowed',
      reasons: ['This site does not put returned medicines back on the shelf.'] },
    ...over,
  }
}

const OPTIONS = {
  stores: [{ code: 'RQ1', name: 'Returns quarantine' }], restock_allowed: false, lane: 'pharmacy',
  may_record_controlled: true, witnesses: [{ id: 'PH2', name: 'Second pharmacist' }],
  decisions: ['write_off', 'restock'], status_labels: {}, number_series: 'PHRET',
}

let routes: Record<string, unknown> = {}
const posts: Array<{ path: string; body: unknown }> = []

beforeEach(() => {
  userId = 'PH1'
  posts.length = 0
  routes = {
    '/pharmacy-store/ward-returns/options': OPTIONS,
    '/pharmacy-store/ward-returns?status=pending_inspection': { ward_returns: [ret()] },
    '/pharmacy-store/ward-returns/returnable': { orders: [{
      id: 'HX-9', patient_id: 'P2', patient_name: 'Ravi Kumar', bed: 'GM-5', ward: 'GM', mrn: 'M2',
      dosage: '1 g', route: 'IV', frequency: 'BD', received_by: 'NUR1', received_by_name: 'Ravi Iyer',
      received_at: '2026-10-09T10:00:00Z', created_at: '2026-10-09T08:00:00Z', med_order_id: null,
      batches: [{ lot_id: 'LOT-2', drug_id: 'MED-2', drug_name: 'Ceftriaxone', strength: '1 g',
        form: 'vial', dispense_unit: 'vials', batch_no: 'C9', expiry_date: '2027-03-01',
        controlled: false, dispensed_units: 4, returned_units: 1, returnable_units: 3 }],
    }], window_days: 30 },
  }
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    if (options?.method === 'POST') {
      const body = JSON.parse(String(options.body))
      posts.push({ path, body })
      if (path.endsWith('/inspect')) {
        return { ward_return: ret({ status: body.decision === 'restock' ? 'restocked' : 'written_off',
          inspected_by: userId, restock: null }) }
      }
      return { ward_return: ret({ id: 8, return_number: 'PHRET-0008' }), replayed: false }
    }
    if (path in routes) return routes[path]
    throw Object.assign(new Error('not stubbed'), { status: 404, detail: { error: 'not_found' } })
  })
})

afterEach(() => { cleanup() })

function show() {
  render(<WardReturnsView meta={{}} openView={() => {}} onModuleOff={() => {}} />)
}

describe('WardReturnsView', () => {
  it('registers as ward-returns behind the write tier', () => {
    expect([viewId, viewLabel, viewOrder, viewPermission])
      .toEqual(['ward-returns', 'Ward returns', 88, 'pharmacy_store.write'])
  })

  it('writes a return off with a reason and never offers restock while the site has not ruled it', async () => {
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(screen.getByText(/This site does not put returned medicines back on the shelf\. A return/))
      .toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Put back on the shelf' })).toBeNull()
    expect(screen.getByText('₹3.00')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Write off' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Write off' }).at(-1)!)
    expect(await screen.findByText('Write the reason for this decision.')).toBeTruthy()
    expect(posts).toEqual([])
    fireEvent.change(screen.getByLabelText('Why is it written off?'), { target: { value: 'Opened strip' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Write off' }).at(-1)!)
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toEqual({ path: '/pharmacy-store/ward-returns/7/inspect',
      body: { decision: 'write_off', reason: 'Opened strip', seal_intact: null } })
    expect(await screen.findByText('Written off.')).toBeTruthy()
  })

  it('asks for a witness before writing off a controlled return', async () => {
    routes['/pharmacy-store/ward-returns?status=pending_inspection'] = {
      ward_returns: [ret({ controlled: true, drug_name: 'Morphine', strength: '10 mg' })] }
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(screen.getByRole('button', { name: 'Write off' }))
    fireEvent.change(screen.getByLabelText('Why is it written off?'), { target: { value: 'Unused' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Write off' }).at(-1)!)
    expect(await screen.findByText(/A second pharmacist witnesses this write-off/)).toBeTruthy()
    expect(posts).toEqual([])
    fireEvent.change(screen.getByLabelText('Witness'), { target: { value: 'PH2' } })
    fireEvent.change(screen.getByLabelText('Witness signing PIN'), { target: { value: '7351' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Write off' }).at(-1)!)
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].body).toMatchObject({ decision: 'write_off', witness_id: 'PH2', witness_pin: '7351' })
  })

  it('offers restock where the site has it, to someone other than the person who took it in', async () => {
    routes['/pharmacy-store/ward-returns/options'] = { ...OPTIONS, restock_allowed: true }
    routes['/pharmacy-store/ward-returns?status=pending_inspection'] = {
      ward_returns: [ret({ restock: { allowed: true, code: null, reasons: [] } })] }
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Put back on the shelf' }))
    fireEvent.change(screen.getByLabelText('What did the inspection find?'), { target: { value: 'Sealed, in date' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Put back on the shelf' }).at(-1)!)
    expect(await screen.findByText(/Confirm the seal is intact/)).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: 'The seal is intact' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Put back on the shelf' }).at(-1)!)
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].body).toEqual({ decision: 'restock', reason: 'Sealed, in date', seal_intact: true })
  })

  it('does not offer restock to the person who took the return in, and shows why a return cannot go back', async () => {
    userId = 'NUR1'
    routes['/pharmacy-store/ward-returns/options'] = { ...OPTIONS, restock_allowed: true }
    routes['/pharmacy-store/ward-returns?status=pending_inspection'] = {
      ward_returns: [ret({ restock: { allowed: true, code: null, reasons: [] } }),
        ret({ id: 9, drug_name: 'Insulin', strength: '', restock: { allowed: false,
          code: 'restock_refused', reasons: ['A refrigerated or frozen medicine is never put back on the shelf once it has left the pharmacy.'] } })] }
    show()
    const opens = await screen.findAllByRole('button', { name: 'Open' })
    fireEvent.click(opens[0])
    expect(screen.queryByRole('button', { name: 'Put back on the shelf' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Write off' })).toBeNull()
    expect(screen.getByText(/You took this return in/)).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: /Open|Close/ })[1])
    expect(await screen.findByText(/refrigerated or frozen medicine is never put back/)).toBeTruthy()
  })

  it('does not offer the pharmacist who took a return in at the counter to write it off', async () => {
    // Strict default for Samuel: a write-off is a second person's decision,
    // as restock is. A return the nurse took in on the ward stays any
    // pharmacist's to write off (the first test above).
    routes['/pharmacy-store/ward-returns?status=pending_inspection'] = {
      ward_returns: [ret({ received_by: 'PH1', received_by_name: 'Test pharmacist' })] }
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(screen.queryByRole('button', { name: 'Write off' })).toBeNull()
    expect(screen.getByText('You took this return in, so another pharmacist inspects it and writes it off.'))
      .toBeTruthy()
    expect(posts).toEqual([])
  })

  it('offers the receiver Write off, never restock, once the site relaxes the second-person rule', async () => {
    // RETURN_WRITE_OFF_NEEDS_SECOND_PERSON = False on the server: the one-line
    // relaxation works on screen too. Restock stays a second person's call.
    routes['/pharmacy-store/ward-returns/options'] = {
      ...OPTIONS, restock_allowed: true, write_off_needs_second_person: false }
    routes['/pharmacy-store/ward-returns?status=pending_inspection'] = {
      ward_returns: [ret({ received_by: 'PH1', received_by_name: 'Test pharmacist',
        restock: { allowed: true, code: null, reasons: [] } })] }
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(screen.getByRole('button', { name: 'Write off' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Put back on the shelf' })).toBeNull()
    expect(screen.getByText('You took this return in, so only another pharmacist can put it back on the shelf.'))
      .toBeTruthy()
  })

  it('takes a return in at the counter with whole units, the batch and a request key', async () => {
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Take in a return' }))
    const row = (await screen.findByText('Ceftriaxone 1 g')).closest('tr') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Take in' }))
    fireEvent.change(screen.getByLabelText('Units coming back'), { target: { value: '4' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Take in' }).at(-1)!)
    expect(await screen.findByText('Enter a whole number from 1 to 3.')).toBeTruthy()
    expect(posts).toEqual([])
    fireEvent.change(screen.getByLabelText('Units coming back'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Seal'), { target: { value: 'yes' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Take in' }).at(-1)!)
    await waitFor(() => expect(posts).toHaveLength(1))
    const { path, body } = posts[0] as { path: string; body: Record<string, unknown> }
    expect(path).toBe('/pharmacy-store/ward-returns')
    expect(body).toMatchObject({ helix_order_id: 'HX-9', quantity_units: 2, lot_id: 'LOT-2',
      seal_intact: true, store_code: 'RQ1' })
    expect(String(body.idempotency_key)).toMatch(/^ward-return-/)
    expect(await screen.findByText(/Taken in: 2 vials of Ceftriaxone 1 g, return PHRET-0008/)).toBeTruthy()
  })
})
