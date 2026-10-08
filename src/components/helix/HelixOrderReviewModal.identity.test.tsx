/**
 * Helix order review, drug identity (grn-6): the substitute picker offers only
 * a drug confirmed for ordering. A draft waiting for a second pharmacist, or a
 * retired drug, is never offered (the server refuses either one anyway). A
 * catalogue row with no identity status, as every row was before the pharmacy
 * store existed, reads as confirmed.
 */
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../../api/client', () => ({
  getHelixOrderSafety: vi.fn(async () => ({
    order_id: 'ORD-1', medication_id: 'MED-AMOX', drug_name: 'Amoxicillin 500mg Oral Capsule',
    is_substitute: false,
    safety: { drug: 'Amoxicillin', allergy_alerts: [], interaction_alerts: [], max_severity: 'none',
      blocking: false, interaction_screened: true, coverage_note: null },
  })),
  transitionHelixOrder: vi.fn(),
  isSafetyAckRequired: () => false,
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
  { id: 'MED-MOX-500', name: 'Mox 500mg Capsule', generic_name: 'Amoxicillin', form: 'Capsule',
    strength: '500mg', identity_status: 'active' },
  { id: 'MED-NOVA', name: 'Novamox 500mg Capsule', generic_name: 'Amoxicillin', form: 'Capsule',
    strength: '500mg', identity_status: 'draft' },
  { id: 'MED-OLD', name: 'Oldmox 500mg Capsule', generic_name: 'Amoxicillin', form: 'Capsule',
    strength: '500mg', identity_status: 'retired' },
  { id: 'MED-LEGACY', name: 'Legacymox 500mg Capsule', generic_name: 'Amoxicillin', form: 'Capsule',
    strength: '500mg' },
]

describe('HelixOrderReviewModal substitute picker', () => {
  it('offers only drugs confirmed for ordering', async () => {
    render(<HelixOrderReviewModal order={order} catalog={catalog} onClose={() => {}} onTransition={() => {}} />)
    await screen.findByText('No allergy or interaction alerts.')
    const options = within(screen.getByRole('combobox')).getAllByRole('option').map(o => o.textContent)
    expect(options).toEqual([
      '(Select substitute)', 'Mox 500mg Capsule (500mg)', 'Legacymox 500mg Capsule (500mg)',
    ])
  })
})
