/**
 * THE SAME MEDICINE ALREADY ORDERED (ruling MO-R6, medorders-9, W9-C).
 *
 * With the medication-order lifecycle on and the server's duplicate rule set
 * to 'acknowledge', a second live order for the same ingredient answers 428
 * with `safety.duplicate_check`. The modal names what is already ordered,
 * says so in its title when nothing else stopped the order, and asks why in
 * the reason box. A screen with no duplicate reads exactly as before.
 */
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { MedicationSafety } from '../../api/client'

let signedIn: { id: string; name: string; role: string } | null = null

vi.mock('../../api/client', () => ({
  getUser: () => signedIn,
}))

import SafetyAlertModal from './SafetyAlertModal'

const DUPLICATE = {
  drug: 'Paracetamol 500 mg',
  allergy_alerts: [],
  interaction_alerts: [],
  max_severity: 'none',
  blocking: true,
  interaction_screened: true,
  coverage_note: null,
  duplicate_check: {
    rule: 'acknowledge',
    decision: 'acknowledge',
    message: 'Paracetamol 500 mg is already ordered for this patient: Paracetamol 1 g, Oral, QDS (prescribed). Give a reason to order the same medicine again.',
    findings: [{
      kind: 'duplicate',
      drug_name: 'Paracetamol',
      description: 'Paracetamol 1 g, Oral, QDS (prescribed)',
      message: 'Already ordered: Paracetamol 1 g, Oral, QDS (prescribed).',
    }],
  },
} as MedicationSafety

const CLEAN: MedicationSafety = {
  drug: 'Paracetamol',
  allergy_alerts: [],
  interaction_alerts: [],
  max_severity: 'none',
  blocking: false,
  interaction_screened: true,
  coverage_note: null,
}

beforeEach(() => { signedIn = { id: 'D1', name: 'Dr One', role: 'doctor' } })

describe('a second order for the same medicine', () => {
  it('names what is already ordered and asks why', () => {
    const onAcknowledge = vi.fn()
    render(
      <SafetyAlertModal safety={DUPLICATE} mode="blocking" onClose={() => {}}
        onAcknowledge={onAcknowledge} />,
    )
    expect(screen.getByRole('heading', { name: 'This medicine is already ordered' })).toBeTruthy()
    expect(screen.getByText('Already ordered')).toBeTruthy()
    expect(screen.getByText('Paracetamol 1 g, Oral, QDS (prescribed)')).toBeTruthy()
    expect(screen.queryByText('No allergy or interaction alerts were raised.')).toBeNull()
    const box = screen.getByLabelText('Clinical justification')
    expect(box.getAttribute('placeholder')).toContain('second order for the same medicine')
    fireEvent.change(box, { target: { value: 'IV while nil by mouth, oral not given' } })
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge & Order' }))
    expect(onAcknowledge).toHaveBeenCalledWith('IV while nil by mouth, oral not given')
  })

  it('reads as before when nothing is already ordered', () => {
    render(<SafetyAlertModal safety={CLEAN} mode="info" onClose={() => {}} />)
    expect(screen.queryByText('Already ordered')).toBeNull()
    expect(screen.getByText('No allergy or interaction alerts were raised.')).toBeTruthy()
  })
})
