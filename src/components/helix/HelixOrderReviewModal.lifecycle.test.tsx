/**
 * Helix order lifecycle in the review modal.
 *
 * A rejection needs a reason (the prescriber is told), a verified or scheduled
 * order can go back to review or be rejected (a stockout found at dispense used
 * to be a dead end), and a step refused because someone moved the order on
 * reloads the order instead of printing "Illegal transition".
 *
 * Dispense quantity and batch (grn-14): only with the pharmacy store module on
 * and the server saying the site may choose, the pharmacist enters the units
 * (bounded by the site maximum) and may pick a batch of the drug that leaves
 * the shelf; a choice the shelf cannot meet is said before Dispense is
 * pressed, and a refusal reads the batches again. With the module off the
 * modal asks for nothing new and Dispense sends what it always sent.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getHelixOrderSafety = vi.fn()
const transitionHelixOrder = vi.fn()
const getHelixDispenseOptions = vi.fn()
const dispenseHelixOrder = vi.fn()
const getHelixOrderTimeline = vi.fn()

vi.mock('../../api/client', () => ({
  getHelixOrderSafety: (...args: unknown[]) => getHelixOrderSafety(...args),
  transitionHelixOrder: (...args: unknown[]) => transitionHelixOrder(...args),
  isSafetyAckRequired: (e: unknown) => !!e && (e as { code?: string }).code === 'safety_ack_required',
}))

vi.mock('../../api/pharmacy', () => ({
  getHelixDispenseOptions: (...args: unknown[]) => getHelixDispenseOptions(...args),
  dispenseHelixOrder: (...args: unknown[]) => dispenseHelixOrder(...args),
  getHelixOrderTimeline: (...args: unknown[]) => getHelixOrderTimeline(...args),
}))

import HelixOrderReviewModal from './HelixOrderReviewModal'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'

const base = {
  id: 'ORD-9',
  patient_name: 'Asha Rao',
  medication_name: 'Amoxicillin 500mg Oral Capsule',
  dosage: '500mg',
  frequency: 'TDS',
  route: 'PO',
  prescribed_by_name: 'Dr Mehta',
  created_at: '2026-10-01 08:00:00',
}

const clean = {
  allergy_alerts: [], interaction_alerts: [], max_severity: 'none', blocking: false,
  interaction_screened: true, coverage_note: null,
}

describe('HelixOrderReviewModal lifecycle', () => {
  beforeEach(() => {
    getHelixOrderSafety.mockReset()
    transitionHelixOrder.mockReset()
    getHelixOrderSafety.mockResolvedValue({ order_id: 'ORD-9', medication_id: 'M', drug_name: base.medication_name, is_substitute: false, safety: clean })
  })

  it('will not reject without a reason, and sends the reason when given', async () => {
    transitionHelixOrder.mockResolvedValue({ ok: true })
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={{ ...base, status: 'reviewing' }} catalog={[]} onClose={() => {}} onTransition={onTransition} />)

    const reject = await screen.findByRole('button', { name: 'Reject' })
    expect(reject).toBeDisabled()
    const hint = screen.getByText('Write the reason in the notes first')
    expect(reject).toHaveAttribute('aria-describedby', hint.id)

    fireEvent.change(screen.getByLabelText('Pharmacist Notes'), { target: { value: 'Duplicate of the charted course' } })
    expect(reject).toBeEnabled()
    fireEvent.click(reject)

    await waitFor(() => expect(onTransition).toHaveBeenCalled())
    expect(transitionHelixOrder).toHaveBeenCalledWith('ORD-9', expect.objectContaining({
      target_status: 'rejected', pharmacist_notes: 'Duplicate of the charted course',
    }))
  })

  it('a scheduled order can go back to review or be rejected', async () => {
    render(<HelixOrderReviewModal order={{ ...base, status: 'scheduled' }} catalog={[]} onClose={() => {}} onTransition={() => {}} />)
    expect(await screen.findByRole('button', { name: 'Dispense' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Return to Review' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })

  it('a 409 swaps in the current order and closing reloads the list', async () => {
    const stale = Object.assign(new Error('This order is now Reviewing.'), {
      status: 409,
      detail: {
        error: 'order_state_changed',
        message: 'This order is now Reviewing. It may have been changed since you opened it; the current state is shown.',
        order: { ...base, status: 'reviewing' },
      },
    })
    transitionHelixOrder.mockRejectedValue(stale)
    const onClose = vi.fn()
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={{ ...base, status: 'pending' }} catalog={[]} onClose={onClose} onTransition={onTransition} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Begin Review' }))
    expect(await screen.findByText(/the current state is shown/)).toBeInTheDocument()
    expect(screen.queryByText(/Illegal transition/)).not.toBeInTheDocument()
    // The steps open from where the order is now.
    expect(screen.getByRole('button', { name: 'Verify' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Begin Review' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onTransition).toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })
})

const options = {
  order_id: 'ORD-9',
  enabled: true,
  max_units: 4,
  medication_id: 'M-AMOX',
  drug_name: 'Amoxicillin 500 mg',
  lots: [
    { id: 'LOT-A', lot_number: 'A12', expiration_date: '2026-12-31', quantity: 2, store_name: 'Main pharmacy' },
    { id: 'LOT-B', lot_number: 'B34', expiration_date: '2027-06-30', quantity: 10, store_name: null },
  ],
}

describe('HelixOrderReviewModal dispense quantity and batch', () => {
  beforeEach(() => {
    getHelixOrderSafety.mockReset()
    transitionHelixOrder.mockReset()
    getHelixDispenseOptions.mockReset()
    dispenseHelixOrder.mockReset()
    getHelixOrderTimeline.mockReset()
    getHelixOrderSafety.mockResolvedValue({ order_id: 'ORD-9', medication_id: 'M', drug_name: base.medication_name, is_substitute: false, safety: clean })
  })

  afterEach(() => {
    act(() => { resetModuleFlags() })
  })

  it('with the pharmacy store off asks for nothing new and dispenses as before', async () => {
    transitionHelixOrder.mockResolvedValue({ ok: true })
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={{ ...base, status: 'scheduled' }} catalog={[]} onClose={() => {}} onTransition={onTransition} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Dispense' }))
    await waitFor(() => expect(onTransition).toHaveBeenCalled())
    expect(getHelixDispenseOptions).not.toHaveBeenCalled()
    expect(dispenseHelixOrder).not.toHaveBeenCalled()
    expect(screen.queryByLabelText(/Units to dispense/)).not.toBeInTheDocument()
    expect(transitionHelixOrder).toHaveBeenCalledWith('ORD-9', { target_status: 'dispensed' })
  })

  it('with the store on, sends the units and the batch the pharmacist chose', async () => {
    act(() => { setModuleFlags({ 'pharmacy-store': true }) })
    getHelixDispenseOptions.mockResolvedValue(options)
    dispenseHelixOrder.mockResolvedValue({ ok: true })
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={{ ...base, status: 'scheduled' }} catalog={[]} onClose={() => {}} onTransition={onTransition} />)

    const units = await screen.findByLabelText('Units to dispense (at most 4)')
    const batch = screen.getByLabelText('Batch of Amoxicillin 500 mg')
    expect(screen.getByRole('option', { name: 'Earliest expiry first' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /^Batch A12, expires .*2026, 2 units, Main pharmacy$/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /^Batch B34, expires .*2027, 10 units$/ })).toBeInTheDocument()

    fireEvent.change(units, { target: { value: '3' } })
    fireEvent.change(batch, { target: { value: 'LOT-B' } })
    fireEvent.click(screen.getByRole('button', { name: 'Dispense' }))
    await waitFor(() => expect(onTransition).toHaveBeenCalled())
    expect(dispenseHelixOrder).toHaveBeenCalledWith('ORD-9', { dispense_quantity_units: 3, lot_id: 'LOT-B' })
    expect(transitionHelixOrder).not.toHaveBeenCalled()
  })

  it('leaves the batch to the earliest expiry unless one is chosen', async () => {
    act(() => { setModuleFlags({ 'pharmacy-store': true }) })
    getHelixDispenseOptions.mockResolvedValue(options)
    dispenseHelixOrder.mockResolvedValue({ ok: true })
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={{ ...base, status: 'scheduled' }} catalog={[]} onClose={() => {}} onTransition={onTransition} />)
    await screen.findByLabelText('Units to dispense (at most 4)')
    fireEvent.click(screen.getByRole('button', { name: 'Dispense' }))
    await waitFor(() => expect(onTransition).toHaveBeenCalled())
    expect(dispenseHelixOrder).toHaveBeenCalledWith('ORD-9', { dispense_quantity_units: 1, lot_id: null })
  })

  it('says why before Dispense is pressed: over the maximum, or a batch too small', async () => {
    act(() => { setModuleFlags({ 'pharmacy-store': true }) })
    getHelixDispenseOptions.mockResolvedValue(options)
    render(<HelixOrderReviewModal order={{ ...base, status: 'scheduled' }} catalog={[]} onClose={() => {}} onTransition={() => {}} />)
    const units = await screen.findByLabelText('Units to dispense (at most 4)')
    const dispense = screen.getByRole('button', { name: 'Dispense' })

    fireEvent.change(units, { target: { value: '5' } })
    expect(dispense).toBeDisabled()
    const hint = screen.getByText('Enter a whole number of units from 1 to 4.')
    expect(dispense).toHaveAttribute('aria-describedby', hint.id)

    fireEvent.change(units, { target: { value: '1.5' } })
    expect(dispense).toBeDisabled()

    fireEvent.change(units, { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('Batch of Amoxicillin 500 mg'), { target: { value: 'LOT-A' } })
    expect(dispense).toBeDisabled()
    expect(screen.getByText(/Batch A12 holds 2 units\. A dispense is never split between batches/)).toBeInTheDocument()

    fireEvent.change(units, { target: { value: '2' } })
    expect(dispense).toBeEnabled()
    expect(dispenseHelixOrder).not.toHaveBeenCalled()
  })

  it('a refusal from the shelf is said and the batches are read again', async () => {
    act(() => { setModuleFlags({ 'pharmacy-store': true }) })
    getHelixDispenseOptions.mockResolvedValue(options)
    const refused = Object.assign(new Error('Batch B34 holds 1 unit; this dispense needs 2 units.'), {
      status: 409,
      detail: { error: 'insufficient_lot_stock', message: 'Batch B34 holds 1 unit; this dispense needs 2 units.' },
    })
    dispenseHelixOrder.mockRejectedValue(refused)
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={{ ...base, status: 'scheduled' }} catalog={[]} onClose={() => {}} onTransition={onTransition} />)
    const units = await screen.findByLabelText('Units to dispense (at most 4)')
    fireEvent.change(units, { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Batch of Amoxicillin 500 mg'), { target: { value: 'LOT-B' } })
    fireEvent.click(screen.getByRole('button', { name: 'Dispense' }))
    expect(await screen.findByText('Batch B34 holds 1 unit; this dispense needs 2 units.')).toBeInTheDocument()
    await waitFor(() => expect(getHelixDispenseOptions).toHaveBeenCalledTimes(2))
    expect(onTransition).not.toHaveBeenCalled()
    // Still a scheduled order: the step was refused, not moved on.
    expect(screen.getByRole('button', { name: 'Dispense' })).toBeInTheDocument()
  })

  it('when the server says this site does not choose, there is no picker', async () => {
    act(() => { setModuleFlags({ 'pharmacy-store': true }) })
    getHelixDispenseOptions.mockResolvedValue({ ...options, enabled: false, max_units: 1, lots: [] })
    transitionHelixOrder.mockResolvedValue({ ok: true })
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={{ ...base, status: 'scheduled' }} catalog={[]} onClose={() => {}} onTransition={onTransition} />)
    await waitFor(() => expect(getHelixDispenseOptions).toHaveBeenCalled())
    expect(screen.queryByLabelText(/Units to dispense/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dispense' }))
    await waitFor(() => expect(onTransition).toHaveBeenCalled())
    expect(transitionHelixOrder).toHaveBeenCalledWith('ORD-9', { target_status: 'dispensed' })
  })

  it('only a scheduled order asks for batches', async () => {
    act(() => { setModuleFlags({ 'pharmacy-store': true }) })
    render(<HelixOrderReviewModal order={{ ...base, status: 'reviewing' }} catalog={[]} onClose={() => {}} onTransition={() => {}} />)
    expect(await screen.findByRole('button', { name: 'Verify' })).toBeInTheDocument()
    expect(getHelixDispenseOptions).not.toHaveBeenCalled()
  })
})
