/**
 * Helix order review: the pharmacist sees the allergy and interaction screen for
 * the drug that would actually be dispensed, a substitute is screened before it
 * is proposed, and a severe alert asks for a reason instead of failing silently.
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

const order = {
  id: 'ORD-1',
  patient_name: 'Asha Rao',
  medication_name: 'Amoxicillin 500mg Oral Capsule',
  dosage: '500mg',
  frequency: 'TDS',
  route: 'PO',
  status: 'reviewing',
  prescribed_by_name: 'Dr Mehta',
  created_at: '2026-10-01 08:00:00',
}

const catalog = [
  { id: 'MED-MONOCEF', name: 'Monocef 1g Injection', generic_name: 'Ceftriaxone', form: 'Vial', strength: '1g' },
]

const severe = {
  drug: 'Monocef 1g Injection (Ceftriaxone)',
  allergy_alerts: [{ type: 'allergy', severity: 'severe', message: 'Patient is allergic to Cephalosporins (documented reaction: anaphylaxis).' }],
  interaction_alerts: [],
  max_severity: 'severe',
  blocking: true,
  interaction_screened: true,
  coverage_note: null,
}

const clean = { ...severe, allergy_alerts: [], max_severity: 'none', blocking: false }

describe('HelixOrderReviewModal drug safety', () => {
  beforeEach(() => {
    getHelixOrderSafety.mockReset()
    transitionHelixOrder.mockReset()
  })

  it('previews the substitute and renders its anaphylaxis alert', async () => {
    getHelixOrderSafety.mockImplementation(async (_id: string, sub?: string) => (
      sub
        ? { order_id: 'ORD-1', medication_id: sub, drug_name: 'Monocef 1g Injection', is_substitute: true, safety: severe }
        : { order_id: 'ORD-1', medication_id: 'MED-AMOX', drug_name: order.medication_name, is_substitute: false, safety: clean }
    ))
    render(<HelixOrderReviewModal order={order} catalog={catalog} onClose={() => {}} onTransition={() => {}} />)

    expect(await screen.findByText('No allergy or interaction alerts.')).toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'MED-MONOCEF' } })
    expect(await screen.findByText(/allergic to Cephalosporins/)).toBeInTheDocument()
    expect(getHelixOrderSafety).toHaveBeenLastCalledWith('ORD-1', 'MED-MONOCEF')
    expect(screen.getByText(/Monocef 1g Injection \(substitute\)/)).toBeInTheDocument()
  })

  it('a 428 opens the reason form and the override re-sends with safety_ack', async () => {
    getHelixOrderSafety.mockResolvedValue({ order_id: 'ORD-1', medication_id: 'MED-MONOCEF', drug_name: 'Monocef 1g Injection', is_substitute: true, safety: severe })
    const ackErr = Object.assign(new Error('Severe drug-safety alerts must be acknowledged'), {
      code: 'safety_ack_required', status: 428, safety: severe,
    })
    transitionHelixOrder.mockRejectedValueOnce(ackErr).mockResolvedValueOnce({ ok: true })
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal order={order} catalog={catalog} onClose={() => {}} onTransition={onTransition} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Verify' }))
    expect(await screen.findByText('Severe safety alert')).toBeInTheDocument()

    const override = screen.getByRole('button', { name: 'Override and verify' })
    expect(override).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Reason for proceeding/), { target: { value: 'Test dose tolerated' } })
    fireEvent.click(override)

    await waitFor(() => expect(onTransition).toHaveBeenCalled())
    expect(transitionHelixOrder).toHaveBeenLastCalledWith('ORD-1', expect.objectContaining({
      target_status: 'verified',
      safety_ack: { acknowledged: true, reason: 'Test dose tolerated' },
    }))
  })

  it('a failed screen never reads as all clear', async () => {
    getHelixOrderSafety.mockRejectedValue(new Error('Failed to fetch'))
    render(<HelixOrderReviewModal order={order} catalog={catalog} onClose={() => {}} onTransition={() => {}} />)
    expect(await screen.findByText(/did not load. Do not assume this drug was checked/)).toBeInTheDocument()
    expect(screen.queryByText('No allergy or interaction alerts.')).not.toBeInTheDocument()
  })
})

describe('HelixOrderReviewModal Suggest Substitute', () => {
  beforeEach(() => {
    getHelixOrderSafety.mockReset()
    transitionHelixOrder.mockReset()
  })

  it('says why it is disabled until a substitute is chosen', async () => {
    getHelixOrderSafety.mockResolvedValue({ order_id: 'ORD-1', medication_id: 'MED-AMOX', drug_name: order.medication_name, is_substitute: false, safety: clean })
    render(<HelixOrderReviewModal order={order} catalog={catalog} onClose={() => {}} onTransition={() => {}} />)
    await screen.findByText('No allergy or interaction alerts.')

    const suggest = screen.getByRole('button', { name: 'Suggest Substitute' })
    expect(suggest).toBeDisabled()
    const hint = screen.getByText('Choose a substitute first')
    expect(suggest).toHaveAttribute('aria-describedby', hint.id)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'MED-MONOCEF' } })
    await waitFor(() => expect(getHelixOrderSafety).toHaveBeenLastCalledWith('ORD-1', 'MED-MONOCEF'))
    const enabled = screen.getByRole('button', { name: 'Suggest Substitute' })
    expect(enabled).toBeEnabled()
    expect(enabled).not.toHaveAttribute('aria-describedby')
    expect(screen.queryByText('Choose a substitute first')).not.toBeInTheDocument()
  })
})
