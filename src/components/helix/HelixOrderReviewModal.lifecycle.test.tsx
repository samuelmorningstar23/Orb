/**
 * Helix order lifecycle in the review modal.
 *
 * A rejection needs a reason (the prescriber is told), a verified or scheduled
 * order can go back to review or be rejected (a stockout found at dispense used
 * to be a dead end), and a step refused because someone moved the order on
 * reloads the order instead of printing "Illegal transition".
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getHelixOrderSafety = vi.fn()
const transitionHelixOrder = vi.fn()

vi.mock('../../api/client', () => ({
  getHelixOrderSafety: (...args: unknown[]) => getHelixOrderSafety(...args),
  transitionHelixOrder: (...args: unknown[]) => transitionHelixOrder(...args),
  isSafetyAckRequired: (e: unknown) => !!e && (e as { code?: string }).code === 'safety_ack_required',
}))

import HelixOrderReviewModal from './HelixOrderReviewModal'

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
