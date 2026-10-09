/**
 * WHAT THE DELIVERIES TAB IS ALLOWED TO SAY.
 *
 * This screen is the ward end of a pharmacy dispense: a nurse signs, in her own
 * name, for a drug that physically arrived. Two ways it can be wrong, and both
 * are about medication sitting in a tray outside a door:
 *
 *   · A FAILED READ THAT LOOKS LIKE A QUIET WARD. "Nothing in transit" and "we
 *     could not find out what is in transit" are opposite facts. If a failure
 *     paints the calm empty state, a nurse walks away from drugs that are
 *     waiting for her. This is the same rule the shift board follows (§6.1).
 *   · A DISPATCH WITH NO NAME ON IT. She is attesting against a specific
 *     dispense by a specific person. That is the whole point of the step —
 *     pharmacy cannot sign for its own dispatch — and it is invisible if the
 *     row does not say whose dispatch it is.
 */
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getOrdersAwaitingReceipt = vi.fn()
const receiveHelixOrder = vi.fn()
// The signed-in account's role, read by lib/roles through getUser. Only the
// ward stock section asks, and only once both switches are on.
let signedInRole = 'nurse'

vi.mock('../../../api/client', () => ({
  getOrdersAwaitingReceipt: (...a: unknown[]) => getOrdersAwaitingReceipt(...a),
  receiveHelixOrder: (...a: unknown[]) => receiveHelixOrder(...a),
  getUser: () => ({ id: 'NUR-1', role: signedInRole }),
}))

// The ward stock client (grn-21), mocked whole: the section must never reach
// it while a switch is off.
const getWardStock = vi.fn()
const getWardTransfersAwaiting = vi.fn()
const listWardIndents = vi.fn()
const raiseWardIndent = vi.fn()
const receiveWardTransfer = vi.fn()
const cancelWardIndent = vi.fn()

vi.mock('../../../api/pharmacyStore/ward', () => ({
  WARD_INDENT_PRIORITY_LABELS: { routine: 'Routine', urgent: 'Urgent', emergency: 'Emergency' },
  getWardStock: (...a: unknown[]) => getWardStock(...a),
  getWardTransfersAwaiting: (...a: unknown[]) => getWardTransfersAwaiting(...a),
  listWardIndents: (...a: unknown[]) => listWardIndents(...a),
  raiseWardIndent: (...a: unknown[]) => raiseWardIndent(...a),
  receiveWardTransfer: (...a: unknown[]) => receiveWardTransfer(...a),
  cancelWardIndent: (...a: unknown[]) => cancelWardIndent(...a),
}))

import DeliveriesSheet from './DeliveriesSheet'
import {
  resetFeatureFlags, resetModuleFlags, setFeatureFlags, setModuleFlags,
} from '../../../shellConfig'

const ORDER = {
  id: 'ORD-1',
  patient_id: 'PAT-1',
  patient_name: 'Anita Rao',
  medication_name: 'Paracetamol',
  strength: '500mg',
  dosage: '500mg',
  route: 'PO',
  frequency: 'TDS',
  ward: 'GM-1',
  bed: 'B-3',
  dispensed_by: 'PHA001',
  dispensed_by_name: 'Meera Nair',
  dispensed_at: '2026-09-07T09:14:00Z',
}

beforeEach(() => {
  getOrdersAwaitingReceipt.mockReset()
  receiveHelixOrder.mockReset()
  for (const fn of [getWardStock, getWardTransfersAwaiting, listWardIndents, raiseWardIndent,
    receiveWardTransfer, cancelWardIndent]) fn.mockReset()
  signedInRole = 'nurse'
})
afterEach(() => {
  vi.clearAllMocks()
  act(() => { resetModuleFlags(); resetFeatureFlags() })
})

const renderSheet = async () => {
  await act(async () => { render(<DeliveriesSheet />) })
}

describe('Deliveries', () => {
  it('names the drug, the patient and who dispensed it', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [ORDER] })
    await renderSheet()
    expect(screen.getByText(/Paracetamol 500mg/)).toBeTruthy()
    expect(screen.getByText(/Anita Rao/)).toBeTruthy()
    expect(screen.getByText(/Dispensed by Meera Nair/)).toBeTruthy()
  })

  it('does not repeat a strength the formulary name already carries', async () => {
    // Seen on the ward, 2026-09-07: "Paracetamol 650mg Oral Tablet 650mg".
    // Most helix_medication_master rows name the strength inside `name`.
    getOrdersAwaitingReceipt.mockResolvedValue({
      orders: [{ ...ORDER, medication_name: 'Paracetamol 650mg Oral Tablet', strength: '650mg' }],
    })
    await renderSheet()
    expect(screen.getByText('Paracetamol 650mg Oral Tablet')).toBeTruthy()
    expect(screen.queryByText(/650mg Oral Tablet 650mg/)).toBeNull()
  })

  it('a failed read never renders as a quiet ward', async () => {
    // THE ASSERTION THAT MATTERS. Not "an error appeared" but "the reassurance
    // did NOT" — a screen can show both and still be lying, and it is the
    // reassurance a nurse acts on.
    getOrdersAwaitingReceipt.mockRejectedValue(new Error('network'))
    await renderSheet()
    expect(screen.getByRole('alert').textContent).toMatch(/could not load/i)
    expect(screen.queryByText(/Nothing in transit/i)).toBeNull()
    expect(screen.getByText(/does not mean nothing is in transit/i)).toBeTruthy()
  })

  it('an empty queue reads as reassurance, not as a failure', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
    await renderSheet()
    expect(screen.getByText(/Nothing in transit/i)).toBeTruthy()
    // The evidence is when it was checked, not a double negative.
    expect(screen.getByText(/^Checked at/).textContent).toMatch(/^Checked at \d{1,2}[:.]\d{2}/)
    expect(screen.queryByText(/Nothing failed to load/i)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('confirming receipt posts and refreshes the queue', async () => {
    getOrdersAwaitingReceipt
      .mockResolvedValueOnce({ orders: [ORDER] })
      .mockResolvedValueOnce({ orders: [] })
    receiveHelixOrder.mockResolvedValue({ ok: true })
    await renderSheet()

    await act(async () => { screen.getByText('Confirm receipt').click() })
    expect(receiveHelixOrder).toHaveBeenCalledWith('ORD-1')
    await waitFor(() => expect(screen.getByText(/Nothing in transit/i)).toBeTruthy())
  })

  it('a refused receipt says so and leaves the row in the queue', async () => {
    // The server refuses when the receiving account is the dispatching account.
    // The row must STAY: the drug is still unsigned-for.
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [ORDER] })
    receiveHelixOrder.mockRejectedValue(
      new Error('The account that dispensed this order cannot also record that the ward received it.'))
    await renderSheet()

    await act(async () => { screen.getByText('Confirm receipt').click() })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/cannot also record/i))
    expect(screen.getByText(/Paracetamol 500mg/)).toBeTruthy()
  })

  it('a missing dose reads as missing, never as a dose', async () => {
    // The Relay handoff writes "REVIEW REQUIRED" when Sage extracted a drug but
    // no reliable dose. Anything that renders that as if it were a regimen is
    // the defect the string exists to prevent.
    getOrdersAwaitingReceipt.mockResolvedValue({
      orders: [{ ...ORDER, dosage: null, route: null, frequency: null }],
    })
    await renderSheet()
    expect(screen.getByText(/No dose recorded/i)).toBeTruthy()
  })

  it('names the substitute that was dispensed, with the prescribed drug and the note', async () => {
    // The nurse signs for what is in the tray. With a substitute on the order
    // the row used to name the prescribed drug instead.
    getOrdersAwaitingReceipt.mockResolvedValue({
      orders: [{
        ...ORDER,
        medication_name: 'Monocef 1g Injection',
        strength: '1g',
        original_medication_name: 'Cefadin 500mg Capsule',
        pharmacist_notes: 'Out of stock, prescriber agreed',
      }],
    })
    await renderSheet()
    expect(screen.getByText('Monocef 1g Injection (substituted for Cefadin 500mg Capsule)')).toBeTruthy()
    expect(screen.getByText('Pharmacist note: Out of stock, prescriber agreed')).toBeTruthy()
  })
})


// ═══ Ward stock (grn-21) ═══════════════════════════════════════════════════
// The ward's own stock store sits below the deliveries, and only while the
// pharmacy store AND ward stock are both switched on (ward stock stays off
// until ruling GRN-RS5 is confirmed) for an account holding ward_stock.read.

const WARD_STORE = {
  id: 7, code: 'WS-CARD', name: 'Cardiology ward stock', department: 'Cardiology',
  ward: 'CARD-1', is_active: true,
}

const PANTO = {
  drug_id: 'MED-PANTO', drug_name: 'Pantoprazole 40mg Oral Tablet', form: 'Tablet',
  strength: '40mg', dispense_unit: null, listed: true, par_units: 20, max_units: null,
  on_hand_units: 6, held_units: 0, expired_units: 0, in_transit_units: 0,
  requested_units: 0, earliest_expiry: '2027-03-31', below_par: true, top_up_units: 14,
  problem: null as { code: string; message: string } | null, lots: [],
}

const OVERVIEW = {
  today: '2026-10-10', stores: [{ store: WARD_STORE, drugs: [PANTO] }],
  may_raise: true, may_receive: true, may_decide: false, may_issue: false,
  priorities: ['routine', 'urgent', 'emergency'],
}

const ON_ITS_WAY = {
  id: 41, transfer_number: 'PHTRF-0041', status: 'dispatched', dispatched_by: 'PHA001',
  dispatched_by_name: 'Meera Nair', dispatched_at: '2026-10-10T08:15:00Z',
  lines: [{
    id: 501, line_no: 1, helix_medication_id: 'MED-PANTO',
    drug_name: 'Pantoprazole 40mg Oral Tablet', batch_no: 'PAN77', expiry_date: '2027-03-31',
    quantity_units: 10, received_units: null,
  }],
}

function switchWardStockOn() {
  act(() => {
    setModuleFlags({ 'pharmacy-store': true })
    setFeatureFlags({ pharmacy_ward_stock: true })
  })
}

function wardAnswers({ awaiting = [ON_ITS_WAY] as unknown[], overview = OVERVIEW } = {}) {
  getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
  getWardStock.mockResolvedValue(overview)
  getWardTransfersAwaiting.mockResolvedValue({ transfers: awaiting })
  listWardIndents.mockResolvedValue({ indents: [] })
}

describe('Ward stock on the Deliveries tab', () => {
  it('is not drawn, and asks the server nothing, while ward stock is off', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
    act(() => { setModuleFlags({ 'pharmacy-store': true }) })
    await renderSheet()
    expect(screen.queryByText('Ward stock')).toBeNull()
    expect(getWardStock).not.toHaveBeenCalled()
    expect(getWardTransfersAwaiting).not.toHaveBeenCalled()
  })

  it('is not drawn while the pharmacy store is off, even with ward stock on', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
    act(() => { setFeatureFlags({ pharmacy_ward_stock: true }) })
    await renderSheet()
    expect(screen.queryByText('Ward stock')).toBeNull()
    expect(getWardStock).not.toHaveBeenCalled()
  })

  it('is not drawn for an account without ward_stock.read', async () => {
    signedInRole = 'doctor'
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
    switchWardStockOn()
    await renderSheet()
    expect(screen.queryByText('Ward stock')).toBeNull()
    expect(getWardStock).not.toHaveBeenCalled()
  })

  it('shows what is on its way, who sent it, and the ward against its list', async () => {
    wardAnswers()
    switchWardStockOn()
    await renderSheet()
    await waitFor(() => expect(screen.getByText('Ward stock')).toBeTruthy())
    expect(screen.getByText('PHTRF-0041')).toBeTruthy()
    expect(screen.getByText(/Sent by Meera Nair/)).toBeTruthy()
    expect(screen.getByText(/6 units on hand of 20/)).toBeTruthy()
    expect(screen.getByText('Below par')).toBeTruthy()
    // The patient-dose deliveries above are unchanged.
    expect(screen.getByText(/Nothing in transit/i)).toBeTruthy()
  })

  it('confirms what arrived in full', async () => {
    wardAnswers()
    receiveWardTransfer.mockResolvedValue({ transfer: { ...ON_ITS_WAY, status: 'received' } })
    switchWardStockOn()
    await renderSheet()
    await waitFor(() => expect(screen.getByText('Confirm what arrived')).toBeTruthy())
    getWardTransfersAwaiting.mockResolvedValue({ transfers: [] })
    await act(async () => { screen.getByText('Confirm what arrived').click() })
    expect(receiveWardTransfer).toHaveBeenCalledWith(41, [
      { line_id: 501, received_units: 10, discrepancy_reason: null }])
    await waitFor(() => expect(screen.getByText(/Receipt recorded in your name/)).toBeTruthy())
  })

  it('a short count needs a reason before it can be confirmed', async () => {
    wardAnswers()
    switchWardStockOn()
    await renderSheet()
    const count = await screen.findByLabelText(/Units of Pantoprazole 40mg Oral Tablet that arrived/)
    await act(async () => { fireEvent.change(count, { target: { value: '8' } }) })
    const confirm = screen.getByText('Confirm what arrived') as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    const why = screen.getByLabelText(/Why fewer units/)
    await act(async () => { fireEvent.change(why, { target: { value: 'Two strips torn' } }) })
    expect(confirm.disabled).toBe(false)
    receiveWardTransfer.mockResolvedValue({ transfer: ON_ITS_WAY })
    await act(async () => { confirm.click() })
    expect(receiveWardTransfer).toHaveBeenCalledWith(41, [
      { line_id: 501, received_units: 8, discrepancy_reason: 'Two strips torn' }])
  })

  it('shows the refusal when the sender tries to receive', async () => {
    wardAnswers()
    // As apiFetch throws a refusal: the status and the server's detail ride on it.
    receiveWardTransfer.mockRejectedValue(Object.assign(
      new Error('you sent this transfer, so somebody else receives it. Nothing was changed.'),
      { status: 403, detail: { error: 'same_person' } }))
    switchWardStockOn()
    await renderSheet()
    await waitFor(() => expect(screen.getByText('Confirm what arrived')).toBeTruthy())
    await act(async () => { screen.getByText('Confirm what arrived').click() })
    await waitFor(() => expect(screen.getAllByRole('alert').some(
      el => /somebody else receives it/.test(el.textContent || ''))).toBe(true))
    expect(screen.getByText('PHTRF-0041')).toBeTruthy()
  })

  it('a failed ward read never renders as nothing on its way', async () => {
    getOrdersAwaitingReceipt.mockResolvedValue({ orders: [] })
    getWardStock.mockRejectedValue(new Error('network'))
    getWardTransfersAwaiting.mockResolvedValue({ transfers: [] })
    listWardIndents.mockResolvedValue({ indents: [] })
    switchWardStockOn()
    await renderSheet()
    await waitFor(() => expect(screen.getByText(/Could not load ward stock/)).toBeTruthy())
    expect(screen.queryByText(/Nothing is on its way to this ward/)).toBeNull()
    expect(screen.getByText(/does not mean nothing is on its way/)).toBeTruthy()
  })

  it('asks the pharmacy for a top-up of a below-par drug on the list', async () => {
    wardAnswers({ awaiting: [] })
    raiseWardIndent.mockResolvedValue({ indent: { id: 9 } })
    switchWardStockOn()
    await renderSheet()
    const pick = await screen.findByRole('checkbox')
    await act(async () => { fireEvent.click(pick) })
    await act(async () => { screen.getByText('Send request').click() })
    expect(raiseWardIndent).toHaveBeenCalledWith({
      store_code: 'WS-CARD',
      lines: [{ helix_medication_id: 'MED-PANTO', quantity_units: 14 }],
      priority: 'routine',
    })
  })

  // A drug classified narcotic or psychotropic after it left the pharmacy: the
  // ward still receives it (it never strands), with a pharmacist witness.
  const MADE_CONTROLLED = {
    ...ON_ITS_WAY,
    controlled: true,
    witness_required: true,
    witnesses: [{ id: 'PHA002', name: 'Ravi Kumar' }],
    lines: [{ ...ON_ITS_WAY.lines[0], ndps: true }],
  }

  it('a delivery made controlled on its way is received only with a witness PIN', async () => {
    wardAnswers({ awaiting: [MADE_CONTROLLED] })
    receiveWardTransfer.mockResolvedValue({ transfer: { ...MADE_CONTROLLED, status: 'received' } })
    switchWardStockOn()
    await renderSheet()
    await waitFor(() => expect(screen.getByText(/became a controlled drug after it was sent/))
      .toBeTruthy())
    expect(screen.getByText(/Now a controlled drug. It is held on the ward, not for use/))
      .toBeTruthy()
    const confirm = screen.getByText('Confirm what arrived') as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Witness'), { target: { value: 'PHA002' } })
    })
    expect(confirm.disabled).toBe(true)
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Witness PIN'), { target: { value: '4826' } })
    })
    expect(confirm.disabled).toBe(false)
    getWardTransfersAwaiting.mockResolvedValue({ transfers: [] })
    await act(async () => { confirm.click() })
    expect(receiveWardTransfer).toHaveBeenCalledWith(41, [
      { line_id: 501, received_units: 10, discrepancy_reason: null }],
    { witness_id: 'PHA002', witness_pin: '4826' })
    await waitFor(() => expect(screen.getByText(/witnessed. The controlled drug is held/))
      .toBeTruthy())
  })

  it('clears the witness PIN after a refusal and shows it', async () => {
    wardAnswers({ awaiting: [MADE_CONTROLLED] })
    receiveWardTransfer.mockRejectedValue(Object.assign(
      new Error("the witness's PIN is not right. Nothing was changed."),
      { status: 403, detail: { error: 'witness_pin_wrong' } }))
    switchWardStockOn()
    await renderSheet()
    const who = await screen.findByLabelText('Witness')
    await act(async () => { fireEvent.change(who, { target: { value: 'PHA002' } }) })
    const pin = screen.getByLabelText('Witness PIN') as HTMLInputElement
    await act(async () => { fireEvent.change(pin, { target: { value: '0000' } }) })
    await act(async () => { screen.getByText('Confirm what arrived').click() })
    await waitFor(() => expect(screen.getAllByRole('alert').some(
      el => /PIN is not right/.test(el.textContent || ''))).toBe(true))
    expect(pin.value).toBe('')
  })

  it('says so when nobody can witness a delivery made controlled on its way', async () => {
    wardAnswers({ awaiting: [{ ...MADE_CONTROLLED, witnesses: [] }] })
    switchWardStockOn()
    await renderSheet()
    await waitFor(() => expect(screen.getByText(/No pharmacist with a signing PIN/)).toBeTruthy())
    expect((screen.getByText('Confirm what arrived') as HTMLButtonElement).disabled).toBe(true)
  })

  it('offers no top-up for a drug the ward may not hold', async () => {
    wardAnswers({
      awaiting: [],
      overview: { ...OVERVIEW, stores: [{ store: WARD_STORE, drugs: [{
        ...PANTO, drug_id: 'MED-INS', drug_name: 'Insulin Aspart 100 units/mL Injection',
        problem: { code: 'high_alert_barred', message: 'Insulin is a high-alert medicine.' },
        top_up_units: 0,
      }] }] },
    })
    switchWardStockOn()
    await renderSheet()
    await waitFor(() => expect(screen.getByText('Insulin is a high-alert medicine.')).toBeTruthy())
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByText('Send request')).toBeNull()
  })
})
