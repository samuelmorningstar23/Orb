/**
 * THE ORDER-TIME SAFETY MODAL (F071, F061).
 *
 *   · Info mode opens AFTER the order was written, so it says that first and,
 *     where the caller can, offers to cancel it. A prescriber used to read an
 *     allergy warning as if nothing had been placed.
 *   · The dose guard now answers on every single-order lane. Its findings are
 *     shown in its own words, a reason the server refused is said, and a dose
 *     block offers no reason box to anyone who cannot clear it.
 */
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { MedicationSafety } from '../../api/client'

let signedIn: { id: string; name: string; role: string } | null = null

// The permission mirror reads the session through getUser, so signing in here
// drives the real permission table.
vi.mock('../../api/client', () => ({
  getUser: () => signedIn,
}))

import SafetyAlertModal from './SafetyAlertModal'

const MODERATE: MedicationSafety = {
  drug: 'Cefuroxime',
  allergy_alerts: [{ severity: 'moderate', message: 'Cefuroxime is a cephalosporin; patient has a penicillin allergy (rash).' }],
  interaction_alerts: [],
  max_severity: 'moderate',
  blocking: false,
  interaction_screened: true,
  coverage_note: null,
}

const DOSE_BLOCK: MedicationSafety = {
  drug: 'Paracetamol',
  allergy_alerts: [],
  interaction_alerts: [],
  max_severity: 'none',
  blocking: true,
  interaction_screened: true,
  coverage_note: null,
  dose_check: {
    decision: 'block',
    override_requires_permission: 'medication.prescribe',
    findings: [{
      kind: 'dose_magnitude', decision: 'block', rule: 'paracetamol',
      message: 'Paracetamol 8 g is at or above 3000 mg, no adult indication reaches this as a single dose.',
    }],
  },
}

beforeEach(() => { signedIn = null })

describe('info mode: the order already exists', () => {
  it('leads with the placed-order sentence and offers to cancel it', () => {
    const onCancelOrder = vi.fn()
    render(
      <SafetyAlertModal
        safety={MODERATE} mode="info" onClose={() => {}}
        placedNotice="This order has been placed as a draft. Cancel it if you did not intend it."
        onCancelOrder={onCancelOrder}
      />,
    )
    const notice = screen.getByRole('status')
    expect(notice.textContent).toBe('This order has been placed as a draft. Cancel it if you did not intend it.')
    // Said before the alert it is about.
    const alert = screen.getByText(/patient has a penicillin allergy/)
    expect(notice.compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel this order' }))
    expect(onCancelOrder).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: 'Keep the order' })).toBeTruthy()
  })

  it('shows why a cancel failed, inside the dialog', () => {
    render(
      <SafetyAlertModal
        safety={MODERATE} mode="info" onClose={() => {}}
        placedNotice="This order has been placed as a draft. Cancel it if you did not intend it."
        onCancelOrder={() => {}} cancelError="Order is already active"
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/The order was not cancelled: Order is already active/)
  })

  it('offers no cancel where the caller has no way to cancel', () => {
    render(<SafetyAlertModal safety={MODERATE} mode="info" onClose={() => {}} placedNotice="Already on the chart." />)
    expect(screen.queryByRole('button', { name: 'Cancel this order' })).toBeNull()
    // The footer button, not the header's close cross (which shares the name).
    expect(screen.getByText('Close', { selector: 'button.safety-btn' })).toBeTruthy()
  })
})

describe('blocking mode: a dose the dose check stopped', () => {
  it('shows the finding in the dose check\'s own words', () => {
    signedIn = { id: 'D1', name: 'Dr A', role: 'doctor' }
    render(<SafetyAlertModal safety={DOSE_BLOCK} mode="blocking" onClose={() => {}} onAcknowledge={() => {}} />)
    expect(screen.getByRole('heading', { name: 'The dose check stopped this order' })).toBeTruthy()
    expect(screen.getByText(/no adult indication reaches this as a single dose/)).toBeTruthy()
    expect(screen.getByText('Stopped')).toBeTruthy()
  })

  it('lets a prescriber write the reason', () => {
    signedIn = { id: 'D1', name: 'Dr A', role: 'doctor' }
    const onAcknowledge = vi.fn()
    render(<SafetyAlertModal safety={DOSE_BLOCK} mode="blocking" onClose={() => {}} onAcknowledge={onAcknowledge} />)
    fireEvent.change(screen.getByLabelText('Clinical justification'), {
      target: { value: 'Loading dose agreed with the consultant' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Acknowledge/ }))
    expect(onAcknowledge).toHaveBeenCalledWith('Loading dose agreed with the consultant')
  })

  it('gives a nurse no reason box, and names who can clear it', () => {
    signedIn = { id: 'N1', name: 'Nurse B', role: 'nurse' }
    render(<SafetyAlertModal safety={DOSE_BLOCK} mode="blocking" onClose={() => {}} onAcknowledge={() => {}} />)
    expect(screen.queryByLabelText('Clinical justification')).toBeNull()
    expect(screen.getByText(/Only a prescriber can authorise a dose the dose check stopped/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Change the dose' })).toBeTruthy()
  })

  it('says when the server refused the written reason', () => {
    signedIn = { id: 'D1', name: 'Dr A', role: 'doctor' }
    render(
      <SafetyAlertModal
        safety={{ ...DOSE_BLOCK, ack_refused: 'a dose override requires a clinical reason' }}
        mode="blocking" onClose={() => {}} onAcknowledge={() => {}}
      />,
    )
    expect(screen.getByText('Not accepted: a dose override requires a clinical reason')).toBeTruthy()
  })
})
